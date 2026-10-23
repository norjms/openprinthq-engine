"""Farm-aware dispatch: hardware matching, automatic retry and queue statistics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.farm_match import (
    hardware_mismatch,
    is_abrasive,
    job_hardware,
    normalize_plate,
)
from backend.app.services.farm_queue import queue_stats, requeue_failed
from backend.app.services.print_scheduler import PrintScheduler


def _status(*nozzles):
    return SimpleNamespace(nozzles=[SimpleNamespace(nozzle_diameter=str(d), nozzle_type=t) for d, t in nozzles])


class TestMatching:
    def test_abrasive_types(self):
        for t in ("PLA-CF", "PA6-GF", "PETG CF", "PAHT-CF", "pla-cf"):
            assert is_abrasive(t), t
        for t in ("PLA", "PETG", "ABS", "CFX", None, ""):
            assert not is_abrasive(t), t

    def test_plate_names_compare_loosely(self):
        assert normalize_plate("Textured PEI Plate") == normalize_plate("textured_pei_plate") == "texturedpei"
        assert normalize_plate("textured pei") == "texturedpei"
        assert normalize_plate(None) == ""

    def test_job_hardware_from_archive_then_library(self):
        item = SimpleNamespace(
            archive=SimpleNamespace(nozzle_diameter=0.6, bed_type=None),
            library_file=SimpleNamespace(file_metadata={"bed_type": "Cool Plate"}),
        )
        hw = job_hardware(item, ["PLA", "PETG-CF"])
        assert hw == {"nozzle_diameter": 0.6, "bed_type": "Cool Plate", "abrasive": ["PETG-CF"]}
        assert job_hardware(SimpleNamespace(archive=None, library_file=None))["nozzle_diameter"] is None

    def test_mismatches_are_positive_only(self):
        job = {"nozzle_diameter": 0.6, "bed_type": "Textured PEI Plate", "abrasive": ["PA-CF"]}
        assert hardware_mismatch(job, _status((0.6, "hardened_steel")), "textured pei") == []
        reasons = hardware_mismatch(job, _status((0.4, "stainless_steel")), "Cool Plate")
        assert len(reasons) == 3
        assert "0.6mm nozzle" in reasons[0] and "hardened" in reasons[1] and "Textured PEI" in reasons[2]
        # unknown data never blocks
        assert hardware_mismatch(job, None, None) == []
        assert hardware_mismatch(job, _status(("", "")), None) == []
        # dual nozzle: either one may match
        assert hardware_mismatch({"nozzle_diameter": 0.6}, _status((0.4, ""), (0.6, "")), None) == []


class _FakeManager:
    def __init__(self, statuses, offline=()):
        self.statuses, self.offline = statuses, set(offline)

    def is_connected(self, pid):
        return pid not in self.offline

    def get_status(self, pid):
        return self.statuses.get(pid)

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name, model="X1C", plate=None):
    p = Printer(
        name=name,
        serial_number=f"t-{name}",
        ip_address="192.0.2.20",
        model=model,
        access_code="00000000",
        installed_plate=plate,
    )
    db.add(p)
    await db.flush()
    return p


class TestSchedulerHardware:
    async def test_picks_the_printer_with_the_right_nozzle(self, db_session, monkeypatch):
        a = await _printer(db_session, "A")
        b = await _printer(db_session, "B")
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _FakeManager({a.id: _status((0.4, "hardened_steel")), b.id: _status((0.6, "hardened_steel"))}),
        )
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        job = {"nozzle_diameter": 0.6, "bed_type": None, "abrasive": []}
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set(), job_hw=job)
        assert pid == b.id and reason is None
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", {b.id}, job_hw=job)
        assert pid is None
        assert "Hardware: A (needs a 0.6mm nozzle" in reason and "Busy: B" in reason

    async def test_plate_and_abrasive_and_no_job_info(self, db_session, monkeypatch):
        a = await _printer(db_session, "A", plate="Cool Plate")
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _FakeManager({a.id: _status((0.4, "stainless_steel"))}),
        )
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        job = {"nozzle_diameter": 0.4, "bed_type": "Textured PEI Plate", "abrasive": ["PLA-CF"]}
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set(), job_hw=job)
        assert pid is None and "hardened nozzle for PLA-CF" in reason and "Textured PEI Plate" in reason
        assert not sched._is_busy_only(reason)  # actionable, so the waiting notification fires
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set())
        assert pid == a.id  # without hardware info nothing changes


class TestRetry:
    async def test_failed_item_is_requeued_until_retries_run_out(self, db_session):
        db_session.add(PrintQueueItem(status="pending", position=5, target_model="X1C"))
        first = PrintQueueItem(
            status="failed",
            position=1,
            target_model="X1C",
            printer_id=None,
            max_retries=2,
            error_message="spaghetti",
            plate_id=2,
        )
        db_session.add(first)
        await db_session.flush()
        copy = await requeue_failed(db_session, first)
        await db_session.commit()
        assert copy.status == "pending" and copy.retry_count == 1 and copy.retry_of_id == first.id
        assert copy.plate_id == 2 and copy.max_retries == 2 and copy.error_message is None
        assert copy.position < 5  # front of the queue
        copy.status = "failed"
        second = await requeue_failed(db_session, copy)
        assert second.retry_count == 2 and second.retry_of_id == first.id
        second.status = "failed"
        assert await requeue_failed(db_session, second) is None

    async def test_no_retry_by_default_or_when_not_failed(self, db_session):
        item = PrintQueueItem(status="failed", position=1)
        db_session.add(item)
        await db_session.flush()
        assert await requeue_failed(db_session, item) is None
        item.status, item.max_retries = "completed", 3
        assert await requeue_failed(db_session, item) is None

    async def test_pinned_job_stays_on_its_printer(self, db_session):
        p = await _printer(db_session, "P")
        item = PrintQueueItem(status="failed", position=1, printer_id=p.id, max_retries=1)
        db_session.add(item)
        await db_session.flush()
        copy = await requeue_failed(db_session, item)
        assert copy.printer_id == p.id


class TestStats:
    async def test_counts_and_rates(self, db_session):
        p = await _printer(db_session, "S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [
            PrintQueueItem(
                status="completed",
                position=1,
                printer_id=p.id,
                created_at=now - timedelta(hours=3),
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1),
            ),
            PrintQueueItem(
                status="failed",
                position=2,
                printer_id=p.id,
                created_at=now - timedelta(hours=3),
                started_at=now - timedelta(hours=3),
                completed_at=now - timedelta(hours=2),
            ),
            PrintQueueItem(status="pending", position=3, retry_count=1),
            PrintQueueItem(status="completed", position=4, created_at=now - timedelta(days=90)),
        ]
        db_session.add_all(rows)
        await db_session.commit()
        st = await queue_stats(db_session, days=30)
        assert st["total"] == 3
        assert st["by_status"] == {"completed": 1, "failed": 1, "pending": 1}
        assert st["success_rate"] == 0.5 and st["retried_items"] == 1
        assert st["avg_print_seconds"] == 3600 and st["avg_wait_seconds"] == 1800
        assert st["printers"] == [
            {"printer_id": p.id, "completed": 1, "failed": 1, "cancelled": 0, "success_rate": 0.5}
        ]


class TestRoutes:
    async def test_mark_preflight_and_stats(self, async_client, db_session, monkeypatch):
        p = await _printer(db_session, "R", plate="Cool Plate")
        archive = PrintArchive(
            filename="a.3mf",
            print_name="A",
            file_path="/tmp/a.3mf",
            file_size=1,
            content_hash="h-farm",
            status="completed",
            nozzle_diameter=0.4,
            bed_type="Textured PEI Plate",
        )
        db_session.add(archive)
        await db_session.flush()
        item = PrintQueueItem(
            status="printing",
            position=1,
            printer_id=p.id,
            archive_id=archive.id,
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        waiting = PrintQueueItem(status="pending", position=2, target_model="X1C", archive_id=archive.id)
        db_session.add_all([item, waiting])
        await db_session.commit()

        r = await async_client.post(
            f"/api/v1/queue/{item.id}/mark", json={"outcome": "failed", "note": "warped", "retry": True}
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "failed" and r.json()["error_message"] == "warped"
        copies = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.retry_of_id == item.id)))
            .scalars()
            .all()
        )
        assert len(copies) == 1 and copies[0].status == "pending"
        r = await async_client.post(f"/api/v1/queue/{item.id}/mark", json={"outcome": "completed"})
        assert r.status_code == 409

        from backend.app.services import printer_manager as pm_module

        fake = _FakeManager({p.id: _status((0.4, "stainless_steel"))})
        monkeypatch.setattr(pm_module, "printer_manager", fake)
        from backend.app.services.print_scheduler import scheduler

        monkeypatch.setattr(scheduler, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        r = await async_client.get(f"/api/v1/queue/{waiting.id}/preflight")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["needs"]["bed_type"] == "Textured PEI Plate"
        row = [x for x in body["printers"] if x["printer_id"] == p.id][0]
        assert row["eligible"] is False and any("Textured PEI Plate" in x for x in row["reasons"])

        r = await async_client.get("/api/v1/queue/stats?days=7")
        assert r.status_code == 200 and r.json()["by_status"].get("failed", 0) >= 1
