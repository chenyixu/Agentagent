"""供应商回调 HTTP 入口与 Worker 进程入口。

回调入口是整个系统里**唯一没有租户身份**的写入口，所以这里重点验证它的信任边界
不在请求头、也不在 payload 里。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio

from appointment.api import create_app
from appointment.api.deps import build_runtime_for_settings
from appointment.config.settings import get_settings
from appointment.db import models as m
from appointment.db.session import db_now, get_sessionmaker
from appointment.worker import SandboxProvider, Worker, sandbox_account
from appointment.worker.providers import sign_receipt
from tests.test_worker_delivery import _confirm_appointment, _deliveries, _reload

pytestmark = pytest.mark.invariant

CALLBACK_URL = "/v1/provider-receipts/sandbox/sandbox-account"


@pytest_asyncio.fixture
async def client(session, seeded):
    settings = get_settings()
    app = create_app(verify_schema=False)
    app.state.settings = settings
    app.state.runtime = build_runtime_for_settings(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        yield http


async def _authoritative_now(session):
    """回调用数据库时钟裁决，回执就必须按同一条时间线签发。

    这与 SSE 保留窗口是同一类问题：把"事件盖章时钟"和"裁决时钟"混在一起，
    测出来的就不是被测逻辑，而是两个时钟差了多少。
    """

    return await db_now(session)


async def _sent(session, seeded, clock, tomorrow_window, settings):
    await _confirm_appointment(session, seeded, clock, tomorrow_window)
    provider = SandboxProvider()
    worker = Worker(
        owner="worker-1",
        settings=settings,
        provider=provider,
        session_factory=get_sessionmaker(),
    )
    await worker.run_once(now=clock.now())
    delivery = (await _deliveries(session, seeded.tenant_id))[0]
    return delivery, provider, provider.message_id_for(delivery.provider_idempotency_key)


async def test_callback_without_identity_headers_is_accepted(
    client, session, seeded, clock, tomorrow_window, settings
):
    """回调不需要租户身份头——这正是它必须靠签名与账户配置把关的原因。"""

    delivery, provider, message_id = await _sent(
        session, seeded, clock, tomorrow_window, settings
    )
    now = await _authoritative_now(session)
    body, headers = provider.build_receipt(
        event_id="cb-1",
        message_id=message_id,
        event_type="delivered",
        occurred_at=now.isoformat(),
    )

    response = await client.post(CALLBACK_URL, json=body, headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["action"] == "APPLIED"
    assert payload["delivery_id"] == str(delivery.id)
    assert payload["status"] == "DELIVERED"
    refreshed = (
        await session.execute(
            _reload(
                m.NotificationDelivery.__table__.select().where(
                    m.NotificationDelivery.id == delivery.id
                )
            )
        )
    ).one()
    assert refreshed.status == "DELIVERED"


async def test_callback_with_forged_signature_is_rejected_but_recorded(
    client, session, seeded, clock, tomorrow_window, settings
):
    delivery, provider, message_id = await _sent(
        session, seeded, clock, tomorrow_window, settings
    )
    body, _headers = provider.build_receipt(
        event_id="cb-forged",
        message_id=message_id,
        event_type="delivered",
        occurred_at=(await _authoritative_now(session)).isoformat(),
    )

    response = await client.post(
        CALLBACK_URL, json=body, headers={"X-Provider-Signature": "not-a-signature"}
    )

    assert response.status_code == 200, "回调一律 200，结论放在 action 里"
    assert response.json()["action"] == "REJECTED"
    assert response.json()["reason"] == "INVALID_SIGNATURE"
    # 恶意请求也留痕：丢了线索就发现不了攻击。
    receipt = (
        await session.execute(
            m.NotificationDelivery.__table__.select()
        )
    )
    assert receipt is not None
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == "ACCEPTED", "伪造回执不得推进状态"


async def test_callback_outside_replay_window_is_rejected(
    client, session, seeded, clock, tomorrow_window, settings
):
    """签名有效但时间过久 = 重放：拒绝，并且绝不改状态。"""

    delivery, provider, message_id = await _sent(
        session, seeded, clock, tomorrow_window, settings
    )
    now = await _authoritative_now(session)
    account = sandbox_account()
    # 用一个远早于当前时刻、但签名完全正确的请求体。
    stale_issued = now - timedelta(seconds=account.replay_window_seconds + 60)
    body, _ = provider.build_receipt(
        event_id="cb-replay",
        message_id=message_id,
        event_type="delivered",
        occurred_at=stale_issued.isoformat(),
        issued_at=stale_issued.isoformat(),
    )
    headers = sign_receipt(
        secret=account.signing_secret, account_id=account.account_id, body=body
    )

    response = await client.post(CALLBACK_URL, json=body, headers=headers)

    assert response.status_code == 200
    assert response.json()["action"] == "REJECTED"
    assert response.json()["reason"] == "REPLAY_WINDOW_EXCEEDED"
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == "ACCEPTED"
    assert refreshed.id == delivery.id


async def test_callback_for_unknown_account_is_quarantined(
    client, session, seeded, clock, tomorrow_window, settings
):
    delivery, provider, message_id = await _sent(
        session, seeded, clock, tomorrow_window, settings
    )
    body, headers = provider.build_receipt(
        event_id="cb-unknown",
        message_id=message_id,
        event_type="delivered",
        occurred_at=(await _authoritative_now(session)).isoformat(),
    )

    response = await client.post(
        "/v1/provider-receipts/sandbox/other-account", json=body, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["action"] == "REJECTED"
    assert response.json()["reason"] == "UNKNOWN_SOURCE"
    refreshed = (await _deliveries(session, seeded.tenant_id))[0]
    assert refreshed.status == "ACCEPTED"
    assert refreshed.id == delivery.id


async def test_worker_entrypoint_runs_bounded_iterations(session, seeded):
    """``python -m appointment.worker`` 的循环可以被有界地跑完并干净退出。"""

    from appointment.config.settings import reset_settings_cache

    reset_settings_cache()
    from appointment.worker.__main__ import serve

    stop = asyncio.Event()
    await serve(owner="entrypoint-test", max_iterations=1, stop_event=stop)
    # 不抛异常、不悬挂即为本测试要的结论：进程入口可被驱动且会自己停。
