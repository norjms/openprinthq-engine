"""Audit log: who changed what through the API, without ever storing bodies."""

from __future__ import annotations

from sqlalchemy import select

from backend.app.models.audit_log import AuditLogEntry
from backend.app.services import audit


class TestActor:
    def test_kinds(self):
        assert audit.actor_of({"x-api-key": "bb_abcdefghijk"}) == ("api_key:bb_abcde", "api_key")
        assert audit.actor_of({"authorization": "Bearer bb_1234567890"}) == ("api_key:bb_12345", "api_key")
        assert audit.actor_of({"x-ophq-actor": "ann@example.com"}) == ("ann@example.com", "gateway")
        assert audit.actor_of({}) == (None, "anonymous")
        assert audit.actor_of({"authorization": "Bearer not-a-jwt"}) == (None, "anonymous")

    def test_jwt_subject(self):
        import jwt

        tok = jwt.encode({"sub": "alice"}, "k", algorithm="HS256")
        assert audit.actor_of({"authorization": f"Bearer {tok}"}) == ("alice", "user")

    def test_what_is_logged(self):
        assert audit.should_log("POST", "/api/v1/printers/1/clear-plate")
        assert audit.should_log("delete", "/api/v1/queue/3")
        assert not audit.should_log("GET", "/api/v1/printers")
        assert not audit.should_log("POST", "/api/v1/metrics")
        assert not audit.should_log("POST", "/not-api")


class TestMiddleware:
    async def test_writes_change_with_route_and_actor(self, async_client, db_session, monkeypatch):
        monkeypatch.setattr(audit, "ENABLED", True)
        r = await async_client.put(
            "/api/v1/failure-detection/policy",
            json={"hold_printer": False, "stall_minutes": 0},
            headers={"X-OPHQ-Actor": "ops@example.com"},
        )
        assert r.status_code == 200
        await async_client.get("/api/v1/failure-detection/policy")
        rows = (await db_session.execute(select(AuditLogEntry))).scalars().all()
        assert len(rows) == 1, [(r.method, r.path) for r in rows]
        e = rows[0]
        assert (e.method, e.path, e.status, e.actor, e.actor_kind) == (
            "PUT",
            "/api/v1/failure-detection/policy",
            200,
            "ops@example.com",
            "gateway",
        )
        assert e.route == "/api/v1/failure-detection/policy"

        r = await async_client.get("/api/v1/audit-log?actor=ops@example.com")
        assert r.status_code == 200 and r.json()[0]["path"] == "/api/v1/failure-detection/policy"
