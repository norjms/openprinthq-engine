"""Per-printer permission scoping (#57), end to end through the real routes.

Fixture farm: printer IN (in scope by id), LOC (in scope by location "Bay 1",
stored as " bay 1 " to prove the match is trimmed and case-insensitive) and
OUT (location "Bay 2", out of scope). The scoped user is in one group limited
to IN plus location "Bay 1"; the unscoped user is in a group with the same
permissions and no scope. Every out-of-scope access must answer 404 so a
scoped user cannot tell a hidden printer from a missing one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from backend.app.core.permissions import ALL_PERMISSIONS


@pytest.fixture
async def farm(async_client: AsyncClient, db_session, printer_factory, archive_factory):
    from backend.app.core.auth import create_access_token, generate_api_key, get_password_hash
    from backend.app.core.printer_scope import clear_api_key_owner_cache
    from backend.app.models.api_key import APIKey
    from backend.app.models.group import Group
    from backend.app.models.settings import Settings
    from backend.app.models.user import User

    clear_api_key_owner_cache()
    db_session.add(Settings(key="auth_enabled", value="true"))
    p_in = await printer_factory(name="In", location="Bay 3")
    p_loc = await printer_factory(name="Loc", location=" bay 1 ")
    p_out = await printer_factory(name="Out", location="Bay 2")

    scoped_group = Group(
        name="Bay 1 crew",
        permissions=list(ALL_PERMISSIONS),
        scope_printer_ids=[p_in.id],
        scope_locations=["Bay 1"],
    )
    open_group = Group(name="Whole farm", permissions=list(ALL_PERMISSIONS))
    db_session.add_all([scoped_group, open_group])
    scoped = User(username="scoped", password_hash=get_password_hash("pw123456"), role="user")
    unscoped = User(username="unscoped", password_hash=get_password_hash("pw123456"), role="user")
    db_session.add_all([scoped, unscoped])
    await db_session.commit()
    for user, group in ((scoped, scoped_group), (unscoped, open_group)):
        await db_session.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g)"), {"u": user.id, "g": group.id}
        )

    keys = {}
    for name, owner in (("scoped_key", scoped.id), ("legacy_key", None)):
        full, key_hash, prefix = generate_api_key()
        db_session.add(
            APIKey(
                name=name,
                key_hash=key_hash,
                key_prefix=prefix,
                user_id=owner,
                can_queue=True,
                can_control_printer=True,
                can_read_status=True,
                enabled=True,
            )
        )
        keys[name] = full
    await db_session.commit()

    a_in = await archive_factory(p_in.id, print_name="in part")
    a_out = await archive_factory(p_out.id, print_name="out part")
    return {
        "in": p_in,
        "loc": p_loc,
        "out": p_out,
        "scoped": scoped,
        "unscoped": unscoped,
        "scoped_group": scoped_group,
        "a_in": a_in,
        "a_out": a_out,
        "h_scoped": {"Authorization": f"Bearer {create_access_token(data={'sub': 'scoped'})}"},
        "h_unscoped": {"Authorization": f"Bearer {create_access_token(data={'sub': 'unscoped'})}"},
        "h_key": {"X-API-Key": keys["scoped_key"]},
        "h_legacy_key": {"X-API-Key": keys["legacy_key"]},
    }


def _ids(resp) -> set[int]:
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


# --- printers: list, detail, control -----------------------------------------


class TestPrinters:
    async def test_list_hides_out_of_scope(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/printers/", headers=farm["h_scoped"]))
        assert ids == {farm["in"].id, farm["loc"].id}

    async def test_unscoped_user_sees_everything(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/printers/", headers=farm["h_unscoped"]))
        assert {farm["in"].id, farm["loc"].id, farm["out"].id} <= ids

    async def test_detail_out_of_scope_is_404(self, async_client, farm):
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}", headers=farm["h_scoped"])
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Printer not found"

    async def test_detail_in_scope_ok(self, async_client, farm):
        for key in ("in", "loc"):
            resp = await async_client.get(f"/api/v1/printers/{farm[key].id}", headers=farm["h_scoped"])
            assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("post", "/print/pause", None),
            ("post", "/print/resume", None),
            ("post", "/print/stop", None),
            ("post", "/gcode", {"command": "G28"}),
            ("post", "/fan-speed", {"fan": "part", "speed": 50}),
            ("post", "/temperature/bed", {"target": 60}),
            ("put", "/out-of-service", {"reason": "x"}),
            ("get", "/status", None),
        ],
    )
    async def test_control_out_of_scope_is_404(self, async_client, farm, method, path, body):
        kwargs = {"headers": farm["h_scoped"]}
        if body is not None:
            kwargs["json"] = body
        resp = await getattr(async_client, method)(f"/api/v1/printers/{farm['out'].id}{path}", **kwargs)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Printer not found"

    async def test_control_in_scope_reaches_the_route(self, async_client, farm):
        # Not connected in tests: the route itself answers, not the scope guard.
        resp = await async_client.post(f"/api/v1/printers/{farm['in'].id}/print/pause", headers=farm["h_scoped"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Printer not connected"

    async def test_out_of_service_toggle_in_scope(self, async_client, farm):
        resp = await async_client.put(
            f"/api/v1/printers/{farm['in'].id}/out-of-service", json={"reason": "nozzle"}, headers=farm["h_scoped"]
        )
        assert resp.status_code == 200, resp.text
        listed = await async_client.get("/api/v1/printers/out-of-service", headers=farm["h_scoped"])
        assert [r["printer_id"] for r in listed.json()] == [farm["in"].id]

    async def test_out_of_service_list_hides_other_printers(self, async_client, farm):
        await async_client.put(
            f"/api/v1/printers/{farm['out'].id}/out-of-service", json={"reason": "x"}, headers=farm["h_unscoped"]
        )
        listed = await async_client.get("/api/v1/printers/out-of-service", headers=farm["h_scoped"])
        assert listed.status_code == 200
        assert listed.json() == []

    async def test_kprofiles_and_sensor_history_out_of_scope_404(self, async_client, farm):
        for path in (
            f"/api/v1/printers/{farm['out'].id}/kprofiles/",
            f"/api/v1/maintenance/printers/{farm['out'].id}",
            f"/api/v1/printers/{farm['out'].id}/alarms/ack",
        ):
            method = async_client.post if path.endswith("/ack") else async_client.get
            resp = await method(path, headers=farm["h_scoped"])
            assert resp.status_code == 404, path


# --- API keys ----------------------------------------------------------------


class TestApiKeys:
    async def test_key_inherits_owner_scope(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/printers/", headers=farm["h_key"]))
        assert ids == {farm["in"].id, farm["loc"].id}
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}", headers=farm["h_key"])
        assert resp.status_code == 404

    async def test_bearer_key_inherits_owner_scope(self, async_client, farm):
        headers = {"Authorization": f"Bearer {farm['h_key']['X-API-Key']}"}
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}", headers=headers)
        assert resp.status_code == 404
        resp = await async_client.get(f"/api/v1/printers/{farm['in'].id}", headers=headers)
        assert resp.status_code == 200

    async def test_ownerless_legacy_key_keeps_full_access(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/printers/", headers=farm["h_legacy_key"]))
        assert farm["out"].id in ids


# --- queue -------------------------------------------------------------------


async def _queue_item(db_session, **kwargs):
    from backend.app.models.print_queue import PrintQueueItem

    item = PrintQueueItem(status="pending", **kwargs)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


class TestQueue:
    async def test_list_hides_other_printers_and_others_unassigned(self, async_client, db_session, farm):
        mine_in = await _queue_item(db_session, printer_id=farm["in"].id, archive_id=farm["a_in"].id)
        other_out = await _queue_item(db_session, printer_id=farm["out"].id, archive_id=farm["a_out"].id)
        mine_unassigned = await _queue_item(
            db_session, target_model="X1C", archive_id=farm["a_in"].id, created_by_id=farm["scoped"].id
        )
        other_unassigned = await _queue_item(
            db_session, target_model="X1C", archive_id=farm["a_in"].id, created_by_id=farm["unscoped"].id
        )
        ids = _ids(await async_client.get("/api/v1/queue/", headers=farm["h_scoped"]))
        assert mine_in.id in ids and mine_unassigned.id in ids
        assert other_out.id not in ids and other_unassigned.id not in ids
        all_ids = _ids(await async_client.get("/api/v1/queue/", headers=farm["h_unscoped"]))
        assert {mine_in.id, other_out.id, mine_unassigned.id, other_unassigned.id} <= all_ids

    async def test_item_routes_out_of_scope_404(self, async_client, db_session, farm):
        item = await _queue_item(db_session, printer_id=farm["out"].id, archive_id=farm["a_out"].id)
        for method, path in (
            ("get", ""),
            ("patch", ""),
            ("delete", ""),
            ("post", "/cancel"),
            ("post", "/start"),
            ("get", "/preflight"),
        ):
            kwargs = {"headers": farm["h_scoped"]}
            if method == "patch":
                kwargs["json"] = {"copies": 1}
            resp = await getattr(async_client, method)(f"/api/v1/queue/{item.id}{path}", **kwargs)
            assert resp.status_code == 404, (method, path, resp.text)

    async def test_item_in_scope_readable(self, async_client, db_session, farm):
        item = await _queue_item(db_session, printer_id=farm["in"].id, archive_id=farm["a_in"].id)
        resp = await async_client.get(f"/api/v1/queue/{item.id}", headers=farm["h_scoped"])
        assert resp.status_code == 200

    async def test_create_on_out_of_scope_printer_refused(self, async_client, farm):
        resp = await async_client.post(
            "/api/v1/queue/",
            json={"archive_id": farm["a_in"].id, "printer_id": farm["out"].id},
            headers=farm["h_scoped"],
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Printer not found"

    async def test_create_on_in_scope_printer_ok(self, async_client, farm):
        resp = await async_client.post(
            "/api/v1/queue/",
            json={"archive_id": farm["a_in"].id, "printer_id": farm["in"].id},
            headers=farm["h_scoped"],
        )
        assert resp.status_code == 200, resp.text

    async def test_create_for_model_with_no_visible_printer_refused(self, async_client, db_session, farm):
        farm["out"].model = "H2D"
        db_session.add(farm["out"])
        await db_session.commit()
        resp = await async_client.post(
            "/api/v1/queue/", json={"archive_id": farm["a_in"].id, "target_model": "H2D"}, headers=farm["h_scoped"]
        )
        assert resp.status_code == 400
        assert "No active printers for model" in resp.json()["detail"]

    async def test_list_filter_by_out_of_scope_printer_is_404(self, async_client, farm):
        resp = await async_client.get(f"/api/v1/queue/?printer_id={farm['out'].id}", headers=farm["h_scoped"])
        assert resp.status_code == 404
        resp = await async_client.get("/api/v1/queue/?printer_id=-1", headers=farm["h_scoped"])
        assert resp.status_code == 200

    async def test_reorder_ignores_out_of_scope_items(self, async_client, db_session, farm):
        item = await _queue_item(db_session, printer_id=farm["out"].id, archive_id=farm["a_out"].id, position=5)
        resp = await async_client.post(
            "/api/v1/queue/reorder", json={"items": [{"id": item.id, "position": 1}]}, headers=farm["h_scoped"]
        )
        assert resp.status_code == 200
        await db_session.refresh(item)
        assert item.position == 5

    async def test_resume_after_failure_out_of_scope_404(self, async_client, farm):
        resp = await async_client.post(f"/api/v1/queue/printer/{farm['out'].id}/resume", headers=farm["h_scoped"])
        assert resp.status_code == 404

    async def test_preflight_dry_run(self, async_client, farm):
        resp = await async_client.post(
            "/api/v1/queue/preflight", json={"printer_id": farm["out"].id}, headers=farm["h_scoped"]
        )
        assert resp.status_code == 404
        resp = await async_client.post("/api/v1/queue/preflight", json={}, headers=farm["h_scoped"])
        assert resp.status_code == 200, resp.text
        names = {p["name"] for p in resp.json()["printers"]}
        assert names == {"In", "Loc"}
        resp = await async_client.post(
            "/api/v1/queue/preflight", json={"printer_id": farm["in"].id}, headers=farm["h_scoped"]
        )
        assert resp.status_code == 200

    async def test_library_add_to_queue_out_of_scope_404(self, async_client, farm):
        resp = await async_client.post(
            "/api/v1/library/files/add-to-queue",
            json={"file_ids": [1], "printer_id": farm["out"].id},
            headers=farm["h_scoped"],
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Printer not found"

    async def test_archive_reprint_query_printer_out_of_scope_404(self, async_client, farm):
        resp = await async_client.post(
            f"/api/v1/archives/{farm['a_in'].id}/reprint?printer_id={farm['out'].id}", headers=farm["h_scoped"]
        )
        assert resp.status_code == 404


# --- archives, history, stats --------------------------------------------------


class TestArchivesAndHistory:
    async def test_archive_list_and_detail(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/archives/", headers=farm["h_scoped"]))
        assert farm["a_in"].id in ids and farm["a_out"].id not in ids
        resp = await async_client.get(f"/api/v1/archives/{farm['a_out'].id}", headers=farm["h_scoped"])
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Archive not found"
        resp = await async_client.get(f"/api/v1/archives/{farm['a_in'].id}", headers=farm["h_scoped"])
        assert resp.status_code == 200

    async def test_archive_list_unscoped(self, async_client, farm):
        ids = _ids(await async_client.get("/api/v1/archives/", headers=farm["h_unscoped"]))
        assert {farm["a_in"].id, farm["a_out"].id} <= ids

    async def test_archive_compare_with_hidden_archive_404(self, async_client, farm):
        resp = await async_client.get(
            f"/api/v1/archives/compare?archive_ids={farm['a_in'].id},{farm['a_out'].id}", headers=farm["h_scoped"]
        )
        assert resp.status_code == 404

    async def test_print_log_and_stats(self, async_client, db_session, farm):
        from backend.app.models.print_log import PrintLogEntry

        e_in = PrintLogEntry(printer_id=farm["in"].id, status="completed", print_name="a")
        e_out = PrintLogEntry(printer_id=farm["out"].id, status="completed", print_name="b")
        db_session.add_all([e_in, e_out])
        await db_session.commit()

        resp = await async_client.get("/api/v1/print-log/", headers=farm["h_scoped"])
        assert resp.status_code == 200, resp.text
        listed = {i["id"]: i["printer_id"] for i in resp.json()["items"]}
        assert e_in.id in listed and e_out.id not in listed
        assert farm["out"].id not in listed.values()
        resp = await async_client.delete(f"/api/v1/print-log/{e_out.id}", headers=farm["h_scoped"])
        assert resp.status_code == 404

        stats = await async_client.get("/api/v1/archives/stats", headers=farm["h_scoped"])
        assert stats.status_code == 200
        assert str(farm["out"].id) not in stats.json()["prints_by_printer"]
        assert str(farm["in"].id) in stats.json()["prints_by_printer"]
        full = await async_client.get("/api/v1/archives/stats", headers=farm["h_unscoped"])
        assert {str(farm["in"].id), str(farm["out"].id)} <= set(full.json()["prints_by_printer"])

        slim = await async_client.get("/api/v1/archives/slim", headers=farm["h_scoped"])
        assert farm["out"].id not in {r["printer_id"] for r in slim.json()}

    async def test_farm_stats(self, async_client, farm):
        from backend.app.services import farm_analytics

        with patch.dict(farm_analytics._current, {farm["in"].id: ("idle", farm_analytics._now())}, clear=True):
            farm_analytics._current[farm["out"].id] = ("idle", farm_analytics._now())
            resp = await async_client.get("/api/v1/stats/farm", headers=farm["h_scoped"])
        assert resp.status_code == 200, resp.text
        assert farm["out"].id not in {p["printer_id"] for p in resp.json()["printers"]}


# --- alarms, failure events, smart plugs, notifications -------------------------


class TestFeeds:
    async def test_alarms(self, async_client, db_session, farm):
        from backend.app.models.printer_alarm import PrinterAlarm

        a_in = PrinterAlarm(printer_id=farm["in"].id, source="hms", code="1", severity="fatal", message="in")
        a_out = PrinterAlarm(printer_id=farm["out"].id, source="hms", code="2", severity="fatal", message="out")
        db_session.add_all([a_in, a_out])
        await db_session.commit()

        ids = _ids(await async_client.get("/api/v1/alarms", headers=farm["h_scoped"]))
        assert ids == {a_in.id}
        summary = (await async_client.get("/api/v1/alarms/summary", headers=farm["h_scoped"])).json()
        assert summary["active"] == 1 and summary["printers"] == [farm["in"].id]
        assert (await async_client.get(f"/api/v1/alarms/{a_out.id}", headers=farm["h_scoped"])).status_code == 404
        assert (await async_client.post(f"/api/v1/alarms/{a_out.id}/ack", headers=farm["h_scoped"])).status_code == 404
        assert (await async_client.get(f"/api/v1/alarms/{a_in.id}", headers=farm["h_scoped"])).status_code == 200
        everything = _ids(await async_client.get("/api/v1/alarms", headers=farm["h_unscoped"]))
        assert {a_in.id, a_out.id} <= everything

    async def test_failure_events(self, async_client, db_session, farm):
        from backend.app.models.failure_event import PrintFailureEvent

        ev = PrintFailureEvent(printer_id=farm["out"].id, source="test", message="m")
        db_session.add(ev)
        await db_session.commit()
        resp = await async_client.get("/api/v1/failure-events", headers=farm["h_scoped"])
        assert resp.status_code == 200 and resp.json() == []
        resp = await async_client.post(f"/api/v1/failure-events/{ev.id}/ack", headers=farm["h_scoped"])
        assert resp.status_code == 404

    async def test_smart_plugs(self, async_client, farm, smart_plug_factory):
        plug_out = await smart_plug_factory(name="out plug", printer_id=farm["out"].id)
        plug_in = await smart_plug_factory(name="in plug", printer_id=farm["in"].id, ip_address="10.10.0.250")
        loose = await smart_plug_factory(name="loose plug", printer_id=None, ip_address="10.10.0.251")
        ids = _ids(await async_client.get("/api/v1/smart-plugs/", headers=farm["h_scoped"]))
        assert ids == {plug_in.id, loose.id}
        resp = await async_client.get(f"/api/v1/smart-plugs/{plug_out.id}", headers=farm["h_scoped"])
        assert resp.status_code == 404
        resp = await async_client.get(f"/api/v1/smart-plugs/{plug_in.id}", headers=farm["h_scoped"])
        assert resp.status_code == 200
        resp = await async_client.get(f"/api/v1/smart-plugs/by-printer/{farm['out'].id}", headers=farm["h_scoped"])
        assert resp.status_code == 404

    async def test_notification_providers(self, async_client, farm, notification_provider_factory):
        p_out = await notification_provider_factory(name="out", printer_id=farm["out"].id)
        p_all = await notification_provider_factory(name="all", printer_id=None)
        ids = _ids(await async_client.get("/api/v1/notifications/", headers=farm["h_scoped"]))
        assert p_all.id in ids and p_out.id not in ids
        resp = await async_client.get(f"/api/v1/notifications/{p_out.id}", headers=farm["h_scoped"])
        assert resp.status_code == 404


# --- camera tokens -----------------------------------------------------------


class TestCamera:
    async def test_stream_token_carries_scope(self, async_client, farm):
        token = (await async_client.post("/api/v1/printers/camera/stream-token", headers=farm["h_scoped"])).json()[
            "token"
        ]
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}/camera/snapshot?token={token}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Printer not found"
        resp = await async_client.get(f"/api/v1/printers/{farm['in'].id}/camera/snapshot?token={token}")
        assert not (resp.status_code == 404 and resp.json().get("detail") == "Printer not found")

    async def test_unscoped_token_reaches_any_printer(self, async_client, farm):
        token = (await async_client.post("/api/v1/printers/camera/stream-token", headers=farm["h_unscoped"])).json()[
            "token"
        ]
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}/camera/snapshot?token={token}")
        assert not (resp.status_code == 404 and resp.json().get("detail") == "Printer not found")

    async def test_camera_status_jwt(self, async_client, farm):
        resp = await async_client.get(f"/api/v1/printers/{farm['out'].id}/camera/status", headers=farm["h_scoped"])
        assert resp.status_code == 404


# --- websocket ---------------------------------------------------------------


class _FakeSocket:
    def __init__(self, visible):
        from types import SimpleNamespace

        self.state = SimpleNamespace(printer_visible_ids=visible, printer_scope_user_id=None)
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class TestWebsocket:
    async def test_ws_token_records_scope_user(self, async_client, farm):
        from backend.app.core.auth import websocket_token_scope_user_id

        token = (await async_client.post("/api/v1/auth/ws-token", headers=farm["h_scoped"])).json()["token"]
        assert await websocket_token_scope_user_id(token) == farm["scoped"].id
        key_token = (await async_client.post("/api/v1/auth/ws-token", headers=farm["h_key"])).json()["token"]
        assert await websocket_token_scope_user_id(key_token) == farm["scoped"].id

    async def test_broadcast_filters_scoped_sockets(self, farm):
        from backend.app.core.websocket import ConnectionManager

        mgr = ConnectionManager()
        scoped = _FakeSocket({farm["in"].id})
        open_ = _FakeSocket(None)
        mgr.active_connections = [scoped, open_]
        await mgr.send_printer_status(farm["out"].id, {"state": "RUNNING"})
        await mgr.send_archive_created({"id": 1, "printer_id": farm["out"].id})
        await mgr.send_printer_status(farm["in"].id, {"state": "IDLE"})
        await mgr.broadcast({"type": "inventory_changed"})
        assert len(open_.sent) == 4
        assert len(scoped.sent) == 2
        assert all(str(farm["out"].id) not in s or '"printer_id": ' + str(farm["in"].id) in s for s in scoped.sent)

    async def test_refresh_recomputes_from_group(self, async_client, db_session, farm):
        from backend.app.core.websocket import ConnectionManager

        mgr = ConnectionManager()
        sock = _FakeSocket(set())
        sock.state.printer_scope_user_id = farm["scoped"].id
        mgr.active_connections = [sock]
        await mgr.refresh_printer_scopes()
        assert sock.state.printer_visible_ids == {farm["in"].id, farm["loc"].id}


# --- groups API --------------------------------------------------------------


class TestGroupsApi:
    @pytest.fixture
    async def admin_headers(self, db_session):
        from backend.app.core.auth import create_access_token, get_password_hash
        from backend.app.models.user import User

        db_session.add(User(username="boss", password_hash=get_password_hash("pw123456"), role="admin"))
        await db_session.commit()
        return {"Authorization": f"Bearer {create_access_token(data={'sub': 'boss'})}"}

    async def test_create_update_clear_scope(self, async_client, farm, admin_headers):
        resp = await async_client.post(
            "/api/v1/groups/",
            json={
                "name": "Night",
                "permissions": [],
                "scope_printer_ids": [farm["out"].id],
                "scope_locations": [" Bay 9 "],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["scope_printer_ids"] == [farm["out"].id]
        assert body["scope_locations"] == ["Bay 9"]

        gid = body["id"]
        resp = await async_client.patch(f"/api/v1/groups/{gid}", json={"description": "x"}, headers=admin_headers)
        assert resp.json()["scope_printer_ids"] == [farm["out"].id]  # omitted = unchanged
        resp = await async_client.patch(
            f"/api/v1/groups/{gid}", json={"scope_printer_ids": None, "scope_locations": None}, headers=admin_headers
        )
        assert resp.json()["scope_printer_ids"] is None and resp.json()["scope_locations"] is None

    async def test_unknown_printer_refused(self, async_client, farm, admin_headers):
        resp = await async_client.post(
            "/api/v1/groups/", json={"name": "Bad", "scope_printer_ids": [999999]}, headers=admin_headers
        )
        assert resp.status_code == 400

    async def test_administrators_cannot_be_scoped(self, async_client, db_session, farm, admin_headers):
        from sqlalchemy import select

        from backend.app.models.group import Group

        admins = (await db_session.execute(select(Group).where(Group.name == "Administrators"))).scalar_one()
        resp = await async_client.patch(
            f"/api/v1/groups/{admins.id}", json={"scope_printer_ids": [farm["in"].id]}, headers=admin_headers
        )
        assert resp.status_code == 400

    async def test_admin_sees_everything(self, async_client, farm, admin_headers):
        ids = _ids(await async_client.get("/api/v1/printers/", headers=admin_headers))
        assert {farm["in"].id, farm["loc"].id, farm["out"].id} <= ids

    async def test_adding_unscoped_group_restores_full_access(self, async_client, db_session, farm):
        from backend.app.models.group import Group

        extra = Group(name="Extra", permissions=[])
        db_session.add(extra)
        await db_session.commit()
        await db_session.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g)"),
            {"u": farm["scoped"].id, "g": extra.id},
        )
        await db_session.commit()
        ids = _ids(await async_client.get("/api/v1/printers/", headers=farm["h_scoped"]))
        assert farm["out"].id in ids


class TestAuthDisabled:
    async def test_no_scoping_without_auth(self, async_client, db_session, farm):
        from sqlalchemy import update

        from backend.app.core.auth import invalidate_auth_enabled_cache
        from backend.app.models.settings import Settings

        await db_session.execute(update(Settings).where(Settings.key == "auth_enabled").values(value="false"))
        await db_session.commit()
        invalidate_auth_enabled_cache()
        ids = _ids(await async_client.get("/api/v1/printers/"))
        assert farm["out"].id in ids
