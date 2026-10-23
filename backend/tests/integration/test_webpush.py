"""Web push: VAPID keys, subscriptions, delivery and pruning (push services mocked)."""

from __future__ import annotations

import base64

from sqlalchemy import select

from backend.app.models.push_subscription import PushSubscription
from backend.app.services import webpush

SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "BPk" + "A" * 84, "auth": "a" * 22}}


async def test_vapid_key_is_made_once(async_client):
    a = (await async_client.get("/api/v1/push/vapid-public-key")).json()["public_key"]
    b = (await async_client.get("/api/v1/push/vapid-public-key")).json()["public_key"]
    assert a == b
    raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
    assert len(raw) == 65 and raw[0] == 4  # uncompressed P-256 point


async def test_subscribe_send_and_prune(async_client, db_session, monkeypatch):
    assert (await async_client.post("/api/v1/push/subscribe", json=SUB)).status_code == 201
    assert (await async_client.post("/api/v1/push/subscribe", json=SUB)).status_code == 201  # idempotent
    other = {**SUB, "endpoint": "https://push.example/gone"}
    await async_client.post("/api/v1/push/subscribe", json=other)
    bad = {**SUB, "endpoint": "http://insecure.example/x"}
    assert (await async_client.post("/api/v1/push/subscribe", json=bad)).status_code == 400

    sent = []

    def fake_send(sub, payload, pem):
        sent.append((sub.endpoint, payload))
        return 410 if sub.endpoint.endswith("/gone") else None

    monkeypatch.setattr(webpush, "_send_one", fake_send)
    ok, msg = await webpush.send_all("Print done", "Cube finished", db=db_session)
    assert ok and "1 expired removed" in msg
    assert {e for e, _ in sent} == {SUB["endpoint"], other["endpoint"]}
    rows = (await db_session.execute(select(PushSubscription))).scalars().all()
    assert [r.endpoint for r in rows] == [SUB["endpoint"]]

    assert (
        await async_client.request("DELETE", "/api/v1/push/subscribe", json={"endpoint": SUB["endpoint"]})
    ).status_code == 200
    ok, msg = await webpush.send_all("t", "m", db=db_session)
    assert not ok and "No browser" in msg


async def test_provider_type_routes_to_webpush(monkeypatch):
    from backend.app.services.notification_service import notification_service

    called = {}

    async def fake_send_all(title, message, url=None, db=None):
        called["args"] = (title, message)
        return True, "Sent to 1 browser(s)"

    monkeypatch.setattr(webpush, "send_all", fake_send_all)
    ok, msg = await notification_service.send_test_notification("webpush", {}, None)
    assert ok and called["args"][0]
