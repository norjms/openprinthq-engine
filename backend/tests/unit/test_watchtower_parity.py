"""Watchtower parity: printers out of service, a free-form maintenance log and
numeric queue priority."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceType, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.farm_queue import requeue_failed
from backend.app.services.print_scheduler import PrintScheduler


def _use_test_db(monkeypatch, test_engine):
    """check_queue opens its own session; point it at the test database."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr("backend.app.services.print_scheduler.async_session", maker)
    monkeypatch.setattr("backend.app.core.database.async_session", maker)


class _FakeManager:
    def __init__(self, offline=()):
        self.offline = set(offline)

    def is_connected(self, pid):
        return pid not in self.offline

    def get_status(self, pid):
        return None  # no live status: hardware checks treat it as unknown

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name, model="X1C", **kw):
    p = Printer(
        name=name,
        serial_number=f"wp-{name}",
        ip_address="192.0.2.30",
        model=model,
        access_code="00000000",
        **kw,
    )
    db.add(p)
    await db.flush()
    return p


class TestOutOfServiceDispatch:
    async def test_model_job_skips_an_out_of_service_printer(self, db_session, monkeypatch):
        a = await _printer(db_session, "A", out_of_service=True, out_of_service_reason="new hotend")
        b = await _printer(db_session, "B")
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr("backend.app.services.print_scheduler.printer_manager", _FakeManager())
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set())
        assert pid == b.id and reason is None
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", {b.id})
        assert pid is None
        assert "Out of service: A" in reason and "Busy: B" in reason
        assert a.id != pid

    async def test_all_out_of_service_is_reported_and_not_busy_only(self, db_session, monkeypatch):
        await _printer(db_session, "Only", out_of_service=True)
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr("backend.app.services.print_scheduler.printer_manager", _FakeManager())
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set())
        assert pid is None and reason == "Out of service: Only"
        assert not sched._is_busy_only(reason)

    async def test_pinned_job_waits_for_its_printer(self, db_session, test_engine, monkeypatch):
        _use_test_db(monkeypatch, test_engine)
        p = await _printer(db_session, "Pinned", out_of_service=True)
        db_session.add(PrintQueueItem(status="pending", position=1, printer_id=p.id))
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr("backend.app.services.print_scheduler.printer_manager", _FakeManager())
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        dispatched = []

        async def _noop(*a, **k):
            return False

        monkeypatch.setattr(sched, "_check_auto_drying", _noop)
        monkeypatch.setattr(sched, "_ensure_ams_mapping", _noop)
        monkeypatch.setattr(sched, "_block_on_filament_deficit", _noop)
        monkeypatch.setattr(sched, "_launch_uploads", lambda ids, printers, limit: dispatched.extend(ids))
        await sched.check_queue()
        assert dispatched == []

        # Control: back in service, the same job goes out.
        p.out_of_service = False
        await db_session.commit()
        await sched.check_queue()
        assert len(dispatched) == 1


class TestOutOfServiceRoutes:
    async def test_take_out_list_and_return(self, async_client, db_session):
        p = await _printer(db_session, "Route")
        await db_session.commit()

        r = await async_client.put(f"/api/v1/printers/{p.id}/out-of-service", json={"reason": "  belt  "})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["printer_id"] == p.id and body["reason"] == "belt" and body["since"]
        since = body["since"]

        # A new reason keeps the original timestamp.
        r = await async_client.put(f"/api/v1/printers/{p.id}/out-of-service", json={"reason": "belt and idler"})
        assert r.json()["reason"] == "belt and idler" and r.json()["since"] == since

        r = await async_client.get("/api/v1/printers/out-of-service")
        assert r.status_code == 200
        assert [x["printer_id"] for x in r.json()] == [p.id]

        r = await async_client.get(f"/api/v1/printers/{p.id}")
        assert r.json()["out_of_service"] is True and r.json()["out_of_service_reason"] == "belt and idler"

        r = await async_client.delete(f"/api/v1/printers/{p.id}/out-of-service")
        assert r.status_code == 200 and r.json()["reason"] is None and r.json()["since"] is None
        assert (await async_client.get("/api/v1/printers/out-of-service")).json() == []

        assert (await async_client.put("/api/v1/printers/999999/out-of-service", json={})).status_code == 404

    async def test_patch_sets_and_clears(self, async_client, db_session):
        p = await _printer(db_session, "Patch")
        await db_session.commit()
        r = await async_client.patch(
            f"/api/v1/printers/{p.id}", json={"out_of_service": True, "out_of_service_reason": "levelling"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["out_of_service"] is True and r.json()["out_of_service_since"]
        r = await async_client.patch(f"/api/v1/printers/{p.id}", json={"out_of_service": False})
        assert r.json()["out_of_service"] is False and r.json()["out_of_service_reason"] is None

    async def test_preflight_names_it(self, async_client, db_session, monkeypatch):
        p = await _printer(db_session, "Pre", out_of_service=True, out_of_service_reason="nozzle swap")
        item = PrintQueueItem(status="pending", position=1, printer_id=p.id)
        db_session.add(item)
        await db_session.commit()
        from backend.app.services import printer_manager as pm_module
        from backend.app.services.print_scheduler import scheduler

        monkeypatch.setattr(pm_module, "printer_manager", _FakeManager())
        monkeypatch.setattr(scheduler, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        r = await async_client.get(f"/api/v1/queue/{item.id}/preflight")
        assert r.status_code == 200, r.text
        row = r.json()["printers"][0]
        assert row["eligible"] is False and "out of service: nozzle swap" in row["reasons"]


class TestMaintenanceLog:
    async def test_free_form_and_scheduled_share_one_history(self, async_client, db_session):
        p = await _printer(db_session, "Log")
        mtype = MaintenanceType(name="Clean nozzle", default_interval_hours=50)
        db_session.add(mtype)
        await db_session.flush()
        pm = PrinterMaintenance(printer_id=p.id, maintenance_type_id=mtype.id)
        db_session.add(pm)
        await db_session.commit()

        r = await async_client.post(f"/api/v1/maintenance/items/{pm.id}/perform", json={"performed_by": "ana"})
        assert r.status_code == 200, r.text
        r = await async_client.post(
            f"/api/v1/maintenance/printers/{p.id}/log",
            json={"title": "Replaced PTFE tube", "category": "Consumable", "notes": "Parts: PTFE 1m"},
        )
        assert r.status_code == 200, r.text
        entry = r.json()
        assert entry["scheduled"] is False and entry["category"] == "consumable"
        assert entry["printer_maintenance_id"] is None and entry["title"] == "Replaced PTFE tube"

        r = await async_client.get(f"/api/v1/maintenance/printers/{p.id}/log")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        sched = [x for x in rows if x["scheduled"]][0]
        assert sched["title"] == "Clean nozzle" and sched["category"] == "scheduled"
        assert sched["performed_by"] == "ana" and sched["printer_id"] == p.id

        # Scheduled rows are not deletable from the log; free-form ones are.
        assert (await async_client.delete(f"/api/v1/maintenance/log/{sched['id']}")).status_code == 409
        assert (await async_client.delete(f"/api/v1/maintenance/log/{entry['id']}")).status_code == 200
        assert len((await async_client.get(f"/api/v1/maintenance/printers/{p.id}/log")).json()) == 1

        # The per-schedule history still works.
        r = await async_client.get(f"/api/v1/maintenance/items/{pm.id}/history")
        assert r.status_code == 200 and len(r.json()) == 1

    async def test_validation(self, async_client, db_session):
        p = await _printer(db_session, "Val")
        await db_session.commit()
        url = f"/api/v1/maintenance/printers/{p.id}/log"
        assert (await async_client.post(url, json={"title": "  "})).status_code == 422
        assert (await async_client.post(url, json={"title": "x", "category": "vibes"})).status_code == 422
        assert (
            await async_client.post("/api/v1/maintenance/printers/999999/log", json={"title": "x"})
        ).status_code == 404
        assert (await async_client.get("/api/v1/maintenance/printers/999999/log")).status_code == 404

    async def test_deleting_the_printer_removes_free_form_rows(self, async_client, db_session):
        p = await _printer(db_session, "Gone")
        await db_session.commit()
        await async_client.post(f"/api/v1/maintenance/printers/{p.id}/log", json={"title": "Lubed rods"})
        r = await async_client.delete(f"/api/v1/printers/{p.id}?delete_archives=false")
        assert r.status_code == 200, r.text
        left = (
            await db_session.execute(select(MaintenanceHistory).where(MaintenanceHistory.printer_id == p.id))
        ).scalars()
        assert list(left) == []


class TestPriority:
    async def test_higher_priority_dispatches_first(self, db_session, test_engine, monkeypatch):
        _use_test_db(monkeypatch, test_engine)
        p = await _printer(db_session, "Prio")
        low = PrintQueueItem(status="pending", position=1, printer_id=p.id, priority=50)
        high = PrintQueueItem(status="pending", position=2, printer_id=p.id, priority=80)
        db_session.add_all([low, high])
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr("backend.app.services.print_scheduler.printer_manager", _FakeManager())
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)

        async def _noop(*a, **k):
            return False

        monkeypatch.setattr(sched, "_check_auto_drying", _noop)
        monkeypatch.setattr(sched, "_ensure_ams_mapping", _noop)
        monkeypatch.setattr(sched, "_block_on_filament_deficit", _noop)
        dispatched = []
        monkeypatch.setattr(sched, "_launch_uploads", lambda ids, printers, limit: dispatched.extend(ids))
        await sched.check_queue()
        assert dispatched == [high.id]

    async def test_api_accepts_and_returns_priority(self, async_client, db_session):
        p = await _printer(db_session, "PrioApi")
        item = PrintQueueItem(status="pending", position=1, printer_id=p.id)
        db_session.add(item)
        await db_session.commit()
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"priority": 90})
        assert r.status_code == 200, r.text
        assert r.json()["priority"] == 90
        assert (await async_client.patch(f"/api/v1/queue/{item.id}", json={"priority": 101})).status_code == 422

    async def test_retry_keeps_priority(self, db_session):
        item = PrintQueueItem(status="failed", position=1, max_retries=1, priority=60, target_model="X1C")
        db_session.add(item)
        await db_session.flush()
        copy = await requeue_failed(db_session, item)
        assert copy.priority == 60


class TestSqliteMigration:
    async def test_rebuilds_old_maintenance_history(self, monkeypatch, tmp_path):
        """The pre-change table had printer_maintenance_id NOT NULL; the migration must
        relax it, keep every row and backfill printer_id."""
        from backend.app.core import database

        monkeypatch.setattr(database, "is_sqlite", lambda: True)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE printers (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(100))"))
            await conn.execute(
                text(
                    "CREATE TABLE printer_maintenance (id INTEGER NOT NULL PRIMARY KEY, printer_id INTEGER NOT NULL, "
                    "maintenance_type_id INTEGER NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE maintenance_history (\n\tid INTEGER NOT NULL, \n\tprinter_maintenance_id INTEGER NOT NULL, "
                    "\n\tperformed_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, \n\thours_at_maintenance FLOAT NOT NULL, "
                    "\n\tnotes TEXT, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(printer_maintenance_id) REFERENCES "
                    "printer_maintenance (id) ON DELETE CASCADE\n)"
                )
            )
            await conn.execute(text("INSERT INTO printers (id, name) VALUES (7, 'p')"))
            await conn.execute(text("INSERT INTO printer_maintenance VALUES (3, 7, 1)"))
            await conn.execute(
                text(
                    "INSERT INTO maintenance_history (id, printer_maintenance_id, hours_at_maintenance, notes) VALUES (1, 3, 12.5, 'old')"
                )
            )
        async with engine.begin() as conn:
            await database._migrate_maintenance_history_log(conn)
            # idempotent
            await database._migrate_maintenance_history_log(conn)
        async with engine.begin() as conn:
            sql = (
                await conn.execute(text("SELECT sql FROM sqlite_master WHERE name='maintenance_history'"))
            ).scalar_one()
            assert "printer_maintenance_id INTEGER NOT NULL" not in sql
            row = (
                await conn.execute(text("SELECT printer_id, hours_at_maintenance, notes FROM maintenance_history"))
            ).one()
            assert tuple(row) == (7, 12.5, "old")
            await conn.execute(
                text("INSERT INTO maintenance_history (printer_id, hours_at_maintenance, title) VALUES (7, 1, 'free')")
            )
        await engine.dispose()
