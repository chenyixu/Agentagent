"""接入层契约：身份、去重、SSE 游标恢复、确认与查单。

这些断言对应设计稿里"对外可验证"的部分：

- 身份只来自请求头，请求体自带 tenant/actor 会被拒绝（§5.1 第 1 步）；
- 同一条消息重复提交不重跑业务效果（§8.2 的两层去重）；
- SSE 断线可用游标恢复；窗口外明确返回 RESET_REQUIRED 而不是假装接上（§9）；
- 确认必须走方案级凭据 + 幂等键，重复提交返回同一订单（§8.3）。

SSE 的流式接口用真实 uvicorn 服务器验证（ASGI 传输层会把响应体缓冲起来，
验不出"真的在流"）。
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from appointment.api import create_app
from appointment.api.deps import build_runtime_for_settings
from appointment.api.identity import IdentityError, resolve_identity
from appointment.api.sse import stream_task_events
from appointment.config.settings import Settings, get_settings
from appointment.core.enums import TaskState, ToolStatus
from appointment.db.session import db_now, get_sessionmaker

pytestmark = pytest.mark.invariant

TENANT_HEADER = "X-Tenant-Id"
ACTOR_HEADER = "X-Actor-Id"
CUSTOMER_HEADER = "X-Customer-Id"
ROLE_HEADER = "X-Role"


# ---------------------------------------------------------------------------
# 身份边界（纯函数，不需要数据库）
# ---------------------------------------------------------------------------
def test_identity_requires_server_side_headers():
    with pytest.raises(IdentityError) as excinfo:
        resolve_identity({}, allow_temp_identity=True)
    assert excinfo.value.status_code == 401


def test_identity_rejects_customer_without_customer_id():
    with pytest.raises(IdentityError):
        resolve_identity(
            {
                TENANT_HEADER: str(uuid4()),
                ACTOR_HEADER: str(uuid4()),
                ROLE_HEADER: "customer",
            },
            allow_temp_identity=True,
        )


def test_identity_rejects_unknown_role_and_bad_uuid():
    tenant, actor = str(uuid4()), str(uuid4())
    with pytest.raises(IdentityError):
        resolve_identity(
            {TENANT_HEADER: tenant, ACTOR_HEADER: actor, ROLE_HEADER: "root"},
            allow_temp_identity=True,
        )
    with pytest.raises(IdentityError):
        resolve_identity(
            {TENANT_HEADER: "not-a-uuid", ACTOR_HEADER: actor},
            allow_temp_identity=True,
        )


def test_identity_is_refused_where_temporary_identity_is_disabled():
    with pytest.raises(IdentityError) as excinfo:
        resolve_identity(
            {TENANT_HEADER: str(uuid4()), ACTOR_HEADER: str(uuid4())},
            allow_temp_identity=False,
        )
    assert excinfo.value.status_code == 501


def test_identity_falls_back_to_generated_request_id():
    ctx = resolve_identity(
        {
            TENANT_HEADER: str(uuid4()),
            ACTOR_HEADER: str(uuid4()),
            CUSTOMER_HEADER: str(uuid4()),
        },
        allow_temp_identity=True,
    )
    assert ctx.request_id
    assert ctx.role == "customer"


# ---------------------------------------------------------------------------
# HTTP 契约
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def client(session, seeded):
    settings = get_settings()
    app = create_app(verify_schema=False)
    # 不走 lifespan：建表与约束校验已由 engine 夹具负责，这里只装运行时。
    app.state.settings = settings
    app.state.runtime = build_runtime_for_settings(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        yield http


def customer_headers(seeded, *, index: int = 0, request_id: str | None = None) -> dict:
    headers = {
        TENANT_HEADER: str(seeded.tenant_id),
        ACTOR_HEADER: str(seeded.customer_actor_ids[index]),
        CUSTOMER_HEADER: str(seeded.customer_ids[index]),
    }
    if request_id:
        headers["X-Request-Id"] = request_id
    return headers


async def test_missing_identity_headers_are_rejected(client, seeded):
    response = await client.post(
        "/v1/messages",
        json={"client_message_id": "m1", "text": "你好", "store_id": str(seeded.store_id)},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_request_body_cannot_carry_its_own_identity(client, seeded):
    """身份只能由服务端从请求头解析：请求体里带 tenant_id 必须被拒绝。

    ``extra="forbid"`` 让"模型/调用方自己想指定租户"在传输层就不可能成立——不是
    到了业务层再判断"你有没有权限"，而是这个字段根本没有入口。
    """

    headers = customer_headers(seeded)
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m1",
            "text": "你好",
            "store_id": str(seeded.store_id),
            "tenant_id": str(uuid4()),
            "actor_id": str(uuid4()),
        },
    )
    assert response.status_code == 400, response.text
    body = response.json()
    # 形状错误也走同一个错误信封，不是 FastAPI 默认的 {"detail": [...]}。
    assert body["error"]["code"] == "VALIDATION_ERROR"
    offending = {
        field["location"] for field in body["error"]["details"]["fields"]
    }
    assert offending == {"body.tenant_id", "body.actor_id"}


async def test_message_submission_returns_task_state_and_cursor(client, seeded):
    headers = customer_headers(seeded)
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m1",
            "text": "我要约肩颈，明天下午三点",
            "store_id": str(seeded.store_id),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_state"] == TaskState.PROPOSED.value
    assert body["task_id"]
    assert body["conversation_id"]
    assert body["event_cursor"] and body["event_cursor"] > 0
    assert [call["tool"] for call in body["tool_calls"]] == [
        "get_service_quote",
        "search_availability",
    ]
    assert "1." in body["reply_text"]


async def test_duplicate_message_does_not_replay_business_effects(client, seeded):
    headers = customer_headers(seeded)
    payload = {
        "client_message_id": "m-dup",
        "text": "我要约肩颈，明天下午三点",
        "store_id": str(seeded.store_id),
    }
    first = await client.post("/v1/messages", headers=headers, json=payload)
    assert first.status_code == 200
    task_id = first.json()["task_id"]

    second = await client.post("/v1/messages", headers=headers, json=payload)
    assert second.status_code == 200
    body = second.json()
    assert body["end_reason"] == "DUPLICATE_MESSAGE"
    assert body["task_id"] == task_id
    assert body["tool_calls"] == []


async def test_same_message_id_with_different_content_is_a_conflict(client, seeded):
    headers = customer_headers(seeded)
    await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-x",
            "text": "你好",
            "store_id": str(seeded.store_id),
        },
    )
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-x",
            "text": "换个说法",
            "store_id": str(seeded.store_id),
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"


async def test_task_snapshot_is_scoped_to_the_customer(client, seeded):
    headers = customer_headers(seeded)
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-snap",
            "text": "我要约肩颈，明天下午三点",
            "store_id": str(seeded.store_id),
        },
    )
    task_id = response.json()["task_id"]

    own = await client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert own.status_code == 200
    snapshot = own.json()
    assert snapshot["state"] == TaskState.PROPOSED.value
    assert snapshot["slots"]["service"]["value"]["service_id"] == str(
        seeded.service_ids["shoulder"]
    )
    assert snapshot["event_cursor"] > 0

    other = customer_headers(seeded, index=1)
    forbidden = await client.get(f"/v1/tasks/{task_id}", headers=other)
    assert forbidden.status_code == 404, "别人的任务必须表现为不存在"


async def test_events_replay_endpoint_matches_persisted_events(client, seeded):
    headers = customer_headers(seeded)
    response = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-replay",
            "text": "我要约肩颈，明天下午三点",
            "store_id": str(seeded.store_id),
        },
    )
    task_id = response.json()["task_id"]
    cursor = response.json()["event_cursor"]

    replay = await client.get(
        f"/v1/tasks/{task_id}/events/replay",
        headers=headers,
        params={"after_sequence": 0},
    )
    assert replay.status_code == 200
    events = replay.json()["events"]
    assert events[-1]["sequence"] == cursor
    types = [event["type"] for event in events]
    assert "candidates_ready" in types
    assert types[-1] == "reply_end"
    # 游标之后为空：不会重复投递已经确认过的事件。
    again = await client.get(
        f"/v1/tasks/{task_id}/events/replay",
        headers=headers,
        params={"after_sequence": cursor},
    )
    assert again.json()["events"] == []


async def test_confirmation_requires_credential_and_is_idempotent(client, seeded):
    headers = customer_headers(seeded)
    first = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-1",
            "text": "我要约肩颈，明天下午三点",
            "store_id": str(seeded.store_id),
        },
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-2",
            "text": "第一个可以",
            "store_id": str(seeded.store_id),
        },
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["task_state"] == TaskState.WAITING_CONFIRMATION.value
    card = body["pending_confirmation"]
    assert card and card["confirmation_token"], body

    # 带错凭据的确认必须被拒（不是"用户说好就算"）。
    bad = await client.post(
        "/v1/confirmations",
        headers=headers,
        json={
            "proposal_id": card["proposal_id"],
            "proposal_version": card["proposal_version"],
            "confirmation_token": "CONFIRM.wrong.token",
            "client_confirmation_event_id": "evt-bad",
            "idempotency_key": "key-bad",
            "expected_task_version": card["expected_task_version"],
        },
    )
    assert bad.status_code == 428
    assert bad.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    payload = {
        "proposal_id": card["proposal_id"],
        "proposal_version": card["proposal_version"],
        "confirmation_token": card["confirmation_token"],
        "client_confirmation_event_id": "evt-1",
        "idempotency_key": "key-1",
        "expected_task_version": card["expected_task_version"],
    }
    confirmed = await client.post("/v1/confirmations", headers=headers, json=payload)
    assert confirmed.status_code == 200, confirmed.text
    committed = confirmed.json()
    assert committed["committed_status"] == "CONFIRMED"
    assert committed["appointment_id"]
    assert committed["replayed"] is False

    # 响应丢失后的重试：同键同参数重放原订单，不产生第二笔。
    #
    # 这条断言守着一个容易踩的顺序问题：确认成功会把 task.version 推进一位，
    # 如果重试先去比 expected_task_version，就会被**自己第一次提交**判成
    # VERSION_CONFLICT——幂等键看起来存在，实际不生效。
    replay = await client.post("/v1/confirmations", headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["appointment_id"] == committed["appointment_id"]
    assert replay.json()["operation_id"] == committed["operation_id"]
    assert replay.json()["replayed"] is True

    # 同一次授权换了幂等键也必须重放原单：凭据是比键更强的去重依据，
    # 换键重复下单在库层会撞 UNIQUE(tenant_id, confirmation_id)。
    new_key = await client.post(
        "/v1/confirmations",
        headers=headers,
        json={**payload, "idempotency_key": "key-1-regenerated"},
    )
    assert new_key.status_code == 200, new_key.text
    assert new_key.json()["appointment_id"] == committed["appointment_id"]

    snapshot = await client.get(f"/v1/tasks/{body['task_id']}", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["appointment"]["appointment_id"] == committed["appointment_id"]

    by_id = await client.get(
        f"/v1/operations/{committed['operation_id']}", headers=headers
    )
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["status"] == "SUCCEEDED"

    # 响应丢失时客户端手里可能只有原幂等键：按作用域键也能查到同一笔操作。
    by_key = await client.get(
        "/v1/operations",
        headers=headers,
        params={"action": "CREATE", "idempotency_key": "key-1"},
    )
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["operation_id"] == committed["operation_id"]
    assert by_key.json()["result"]["appointment_id"] == committed["appointment_id"]


async def test_unknown_operation_result_is_polled_not_retried(client, session, seeded):
    """结果不明 → 202 + 原操作 ID，让调用方去查，而不是换个键重来。

    这是"最多一次业务效果"的关键分支：如果这一支返回 4xx 引导重试，客户端就会
    用一个新键再下一次单，于是两张订单。
    """

    from sqlalchemy import select

    from appointment.db import models as m

    headers = customer_headers(seeded)
    await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-unk-1",
            "text": "我要约肩颈，明天下午三点",
            "store_id": str(seeded.store_id),
        },
    )
    second = await client.post(
        "/v1/messages",
        headers=headers,
        json={
            "client_message_id": "m-unk-2",
            "text": "第一个可以",
            "store_id": str(seeded.store_id),
        },
    )
    card = second.json()["pending_confirmation"]
    payload = {
        "proposal_id": card["proposal_id"],
        "proposal_version": card["proposal_version"],
        "confirmation_token": card["confirmation_token"],
        "client_confirmation_event_id": "evt-unk",
        "idempotency_key": "key-unk",
        "expected_task_version": card["expected_task_version"],
    }
    first = await client.post("/v1/confirmations", headers=headers, json=payload)
    assert first.status_code == 200, first.text

    # 模拟"写已落库但结果未记账"：账本状态停在 UNKNOWN。
    operation = (
        await session.execute(
            select(m.Operation).where(
                m.Operation.tenant_id == seeded.tenant_id,
                m.Operation.idempotency_key == "key-unk",
            )
        )
    ).scalar_one()
    operation.status = "UNKNOWN"
    await session.commit()

    retry = await client.post("/v1/confirmations", headers=headers, json=payload)
    assert retry.status_code == 202, retry.text
    body = retry.json()
    assert body["error"]["code"] == "UNKNOWN_OUTCOME"
    assert body["error"]["operation_id"] == str(operation.id)
    assert body["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# SSE：游标恢复、保留窗口、流式关闭
# ---------------------------------------------------------------------------
async def _collect(gen, *, limit: int = 50) -> list[str]:
    frames: list[str] = []
    async for frame in gen:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


def _frame_names(frames: list[str]) -> list[str]:
    return [
        line[len("event: ") :]
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("event: ")
    ]


async def test_stream_replays_from_cursor_and_closes_at_reply_end(session, seeded):
    from appointment.orchestrator import Orchestrator
    from appointment.agent import DeterministicRuntime
    from appointment.core.clock import FrozenClock
    from tests.conftest import customer_ctx, REFERENCE_NOW

    clock = FrozenClock(REFERENCE_NOW)
    ctx = customer_ctx(seeded)
    orchestrator = Orchestrator(
        session, runtime=DeterministicRuntime(), clock=clock
    )
    report = await orchestrator.handle_user_message(
        ctx,
        text="我要约肩颈，明天下午三点",
        client_message_id="m-sse",
        store_id=seeded.store_id,
        now=clock.now(),
    )
    await session.commit()

    factory = get_sessionmaker()

    async def _now(session):
        # 事件是用冻结时钟盖章的，裁决时刻就用同一条时间线。
        # 混用两个时钟测的就不是"游标恢复"，而是"容器时钟漂移了多少"。
        return REFERENCE_NOW

    frames = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=report.task_id,
            after_sequence=0,
            retention_events=500,
            retention_seconds=3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    names = _frame_names(frames)
    assert "candidates_ready" in names
    assert names[-1] == "reply_end", names
    # 每个业务事件都带自己的 sequence 作为 SSE id，浏览器重连自动带回。
    ids = [
        line[len("id: ") :]
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("id: ")
    ]
    assert ids == [str(i) for i in range(1, len(frames) + 1)]

    # 已经追平：立刻收到 caught_up 就结束，不会挂住连接。
    caught_up = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=report.task_id,
            after_sequence=report.event_cursor or 0,
            retention_events=500,
            retention_seconds=3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    assert _frame_names(caught_up) == ["caught_up"]


async def test_stream_outside_retention_requires_reset(session, seeded):
    from appointment.orchestrator import Orchestrator
    from appointment.agent import DeterministicRuntime
    from appointment.core.clock import FrozenClock
    from tests.conftest import customer_ctx, REFERENCE_NOW

    clock = FrozenClock(REFERENCE_NOW)
    orchestrator = Orchestrator(
        session, runtime=DeterministicRuntime(), clock=clock
    )
    # 第一轮：T0。这一轮的事件之后会被"时间窗口"整段裁掉。
    first = await orchestrator.handle_user_message(
        customer_ctx(seeded),
        text="我要约肩颈，明天下午三点",
        client_message_id="m-sse2",
        store_id=seeded.store_id,
        now=clock.now(),
    )
    await session.commit()

    # 第二轮：T0+2h，用户选定方案。冻结时钟下同一轮事件共用一个时刻，
    # 所以必须有第二轮，时间维度才有可区分的时间戳可测。
    clock.advance(hours=2)
    await orchestrator.handle_user_message(
        customer_ctx(seeded),
        text="第一个可以",
        client_message_id="m-sse2b",
        store_id=seeded.store_id,
        now=clock.now(),
    )
    await session.commit()

    factory = get_sessionmaker()

    async def _now(session):
        # 裁决时刻固定为 T0+2h：测的是保留策略本身，不是容器时钟。
        return clock.now()

    # 条数维度：策略上只保证最近 1 条可回放，游标 0 必然落在窗口之外。
    frames = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=first.task_id,
            after_sequence=0,
            retention_events=1,
            retention_seconds=3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    assert _frame_names(frames) == ["RESET_REQUIRED"]
    data = json.loads(frames[0].split("data: ", 1)[1])
    assert data["reason"] == "CURSOR_OUT_OF_RETENTION"
    assert data["retained_from"] > 1

    # 时间维度：窗口 1 小时。第一轮事件在 T0，已经比 T0+1h 的截止线更早，
    # 因此从游标 0 恢复会被拒绝，且下界正好是第二轮的第一条事件。
    frames = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=first.task_id,
            after_sequence=0,
            retention_events=500,
            retention_seconds=3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    assert _frame_names(frames) == ["RESET_REQUIRED"]
    data = json.loads(frames[0].split("data: ", 1)[1])
    assert data["reason"] == "CURSOR_OUT_OF_RETENTION"
    assert data["retained_from"] == (first.event_cursor or 0) + 1, (
        "时间窗口应恰好裁掉第一轮整段事件"
    )

    # 对照组：同样的事件、同样的游标，窗口放大到覆盖全部历史时就能完整回放。
    # 这样上面那条断言证明的是"窗口生效"，而不是"事件本来就取不到"。
    frames = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=first.task_id,
            after_sequence=0,
            retention_events=500,
            retention_seconds=24 * 3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    names = _frame_names(frames)
    assert names[0] != "RESET_REQUIRED"
    assert "candidates_ready" in names
    assert names[-1] == "reply_end"
    # 从 sequence 1 开始完整回放，一个都没被裁掉。
    assert frames[0].startswith("id: 1\n")

    # 游标超过尾部（拿了别的任务的游标）也必须要求重来，而不是空等。
    frames = await _collect(
        stream_task_events(
            factory,
            tenant_id=seeded.tenant_id,
            task_id=first.task_id,
            after_sequence=(first.event_cursor or 0) + 500,
            retention_events=500,
            retention_seconds=3600,
            now_provider=_now,
            poll_interval=0.01,
        )
    )
    assert _frame_names(frames) == ["RESET_REQUIRED"]
    data = json.loads(frames[0].split("data: ", 1)[1])
    assert data["reason"] == "CURSOR_AHEAD_OF_TASK"


async def test_stream_over_real_http_is_incremental(session, seeded, monkeypatch):
    """真实 uvicorn 上验证：SSE 是真流式，且带正确的 Content-Type。"""

    import uvicorn

    import appointment.api.app as api_app

    monkeypatch.setattr(api_app, "dispose_engine", _noop_async)

    settings = get_settings()
    app = create_app(verify_schema=False)
    app.state.settings = settings
    app.state.runtime = build_runtime_for_settings(settings)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        await _wait_for_server(port)
        headers = customer_headers(seeded)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            submitted = await http.post(
                "/v1/messages",
                headers=headers,
                json={
                    "client_message_id": "m-live",
                    "text": "我要约肩颈，明天下午三点",
                    "store_id": str(seeded.store_id),
                },
            )
            assert submitted.status_code == 200, submitted.text
            task_id = submitted.json()["task_id"]

            names: list[str] = []
            async with http.stream(
                "GET",
                f"/v1/tasks/{task_id}/events",
                headers=headers,
                params={"after_sequence": 0},
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        names.append(line[len("event: ") :])
                    if names and names[-1] == "reply_end":
                        break
            assert "candidates_ready" in names
            assert names[-1] == "reply_end"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def _noop_async() -> None:
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for_server(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("uvicorn 未能在预期时间内启动")
