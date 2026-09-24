from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from appointment.api import create_app
from appointment.config.settings import get_settings
from webapp.booking_api import install_booking_api
from webapp.identity import BusinessIdentity, IdentityRegistry


@pytest.mark.asyncio
async def test_browser_booking_context_uses_server_registry_and_rejects_unknown_user():
    tenant_id, actor_id, customer_id, store_id = (uuid4() for _ in range(4))
    registry = IdentityRegistry(
        {
            "customer-1": BusinessIdentity(
                user_id="customer-1",
                role="customer",
                display_name="演示顾客",
                tenant_id=tenant_id,
                actor_id=actor_id,
                customer_id=customer_id,
                store_id=store_id,
            ),
            "manager": BusinessIdentity(
                user_id="manager",
                role="store_manager",
                display_name="店长",
                tenant_id=tenant_id,
                actor_id=uuid4(),
                customer_id=None,
                store_id=store_id,
            ),
        }
    )
    app = FastAPI()
    app.state.appointment_identity_registry = registry
    install_booking_api(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        context = await client.get(
            "/booking/v1/context", headers={"X-User-ID": "customer-1"}
        )
        unknown = await client.get(
            "/booking/v1/context", headers={"X-User-ID": "customer-unknown"}
        )
        manager = await client.get(
            "/booking/v1/context", headers={"X-User-ID": "manager"}
        )
        missing = await client.get("/booking/v1/context")

    assert context.status_code == 200
    assert context.json() == {"store_id": str(store_id)}
    assert unknown.status_code == 401
    assert manager.status_code == 403
    assert missing.status_code == 401


@pytest.mark.asyncio
async def test_browser_booking_can_restore_confirmation_and_commit_once(seeded):
    identity = BusinessIdentity(
        user_id="customer-1",
        role="customer",
        display_name="演示顾客",
        tenant_id=seeded.tenant_id,
        actor_id=seeded.customer_actor_ids[0],
        customer_id=seeded.customer_ids[0],
        store_id=seeded.store_id,
    )
    app = create_app(verify_schema=False)
    app.state.settings = get_settings()
    app.state.appointment_identity_registry = IdentityRegistry({"customer-1": identity})
    install_booking_api(app)

    transport = httpx.ASGITransport(app=app)
    headers = {"X-User-ID": "customer-1"}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/booking/v1/messages",
            headers=headers,
            json={
                "client_message_id": "browser-msg-1",
                "text": "我要约肩颈，明天下午三点",
                "store_id": str(seeded.store_id),
            },
        )
        assert first.status_code == 200, first.text
        second = await client.post(
            "/booking/v1/messages",
            headers=headers,
            json={
                "client_message_id": "browser-msg-2",
                "text": "第一个可以",
                "store_id": str(seeded.store_id),
            },
        )
        assert second.status_code == 200, second.text
        task_id = second.json()["task_id"]
        assert second.json()["task_state"] == "WAITING_CONFIRMATION"

        event_stream = await client.get(
            f"/booking/v1/tasks/{task_id}/events",
            params={"after_sequence": first.json()["event_cursor"]},
            headers=headers,
        )
        assert event_stream.status_code == 200
        assert event_stream.headers["content-type"].startswith("text/event-stream")
        assert "event: reply_end" in event_stream.text

        snapshot = await client.get(f"/booking/v1/tasks/{task_id}", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["appointment"] is None

        credential = await client.post(
            f"/booking/v1/tasks/{task_id}/confirmation-credential", headers=headers
        )
        assert credential.status_code == 200, credential.text
        assert credential.headers["cache-control"] == "no-store"
        card = credential.json()

        confirmed = await client.post(
            "/booking/v1/confirmations",
            headers=headers,
            json={
                "proposal_id": card["proposal_id"],
                "proposal_version": card["proposal_version"],
                "confirmation_token": card["confirmation_token"],
                "client_confirmation_event_id": "browser-confirm-event-1",
                "idempotency_key": "browser-confirm-key-1",
                "expected_task_version": card["expected_task_version"],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["appointment_id"]
        assert confirmed.json()["replayed"] is False

        committed_events = await client.get(
            f"/booking/v1/tasks/{task_id}/events/replay",
            params={"after_sequence": second.json()["event_cursor"]},
            headers=headers,
        )
        assert committed_events.status_code == 200
        assert any(
            event["type"] == "appointment_committed"
            for event in committed_events.json()["events"]
        )

        final_snapshot = await client.get(f"/booking/v1/tasks/{task_id}", headers=headers)
        assert final_snapshot.status_code == 200
        assert final_snapshot.json()["appointment"]["appointment_id"] == confirmed.json()["appointment_id"]
