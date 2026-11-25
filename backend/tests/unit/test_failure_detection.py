"""Engine-native failure detection: events, the hold policy, feeds, stall detection, routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.failure_event import PrintFailureEvent
from backend.app.models.settings import Settings
from backend.app.services import failure_detection as fd


@pytest.fixture
def wired(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(fd.database, "async_session", maker)
    fd.HELD.clear()
    yield maker
    fd.HELD.clear()


async def _set(maker, key, value):
    async with maker() as db:
        db.add(Settings(key=key, value=value))
        await db.commit()


class TestEvents:
    async def test_bambu_detection_codes_only(self, wired):
        evs = await fd.on_bambu_hms(3, ["0C00_8042", "0700_8011", "0300_8003"])
        assert [e.code for e in evs] == ["0C00_8042", "0300_8003"]
        assert all(e.source == "bambu_hms" for e in evs)
        assert not fd.is_held(3)  # hold is off by default

    async def test_hold_and_acknowledge(self, wired):
        await _set(wired, "failure_hold_printer", "true")
        ev = await fd.record_event(5, "obico", "spaghetti", score=0.9, action_taken="pause")
        assert fd.is_held(5) and ev.action_taken == "pause+hold"
        await fd.record_event(5, "stall", "stuck")
        assert await fd.acknowledge([ev.id], None, "op") == 1
        assert fd.is_held(5)  # one event still open
        assert await fd.acknowledge(None, 5, "op") == 1
        assert not fd.is_held(5)
        async with wired() as db:
            rows = (await db.execute(select(PrintFailureEvent))).scalars().all()
        assert all(r.acknowledged_at is not None and r.acknowledged_by == "op" for r in rows)

    async def test_holds_reload_at_startup(self, wired):
        await _set(wired, "failure_hold_printer", "true")
        await fd.record_event(8, "klipper", "MCU shutdown")
        fd.HELD.clear()
        await fd.load_holds()
        assert fd.is_held(8)


class TestStall:
    def test_fires_once_after_the_window(self):
        t = [0.0]
        det = fd.StallDetector(clock=lambda: t[0])
        st = SimpleNamespace(state="RUNNING", progress=40.0, layer_num=12)
        assert det.observe(1, st, 10) is False
        t[0] = 9 * 60
        assert det.observe(1, st, 10) is False
        t[0] = 10 * 60 + 1
        assert det.observe(1, st, 10) is True
        t[0] = 30 * 60
        assert det.observe(1, st, 10) is False  # only once per stall

    def test_progress_resets_and_pause_or_off_exempt(self):
        t = [0.0]
        det = fd.StallDetector(clock=lambda: t[0])
        det.observe(1, SimpleNamespace(state="RUNNING", progress=1.0, layer_num=1), 5)
        t[0] = 4 * 60
        det.observe(1, SimpleNamespace(state="RUNNING", progress=2.0, layer_num=1), 5)
        t[0] = 8 * 60
        assert det.observe(1, SimpleNamespace(state="RUNNING", progress=2.0, layer_num=1), 5) is False
        assert det.observe(1, SimpleNamespace(state="PAUSE", progress=2.0, layer_num=1), 5) is False
        assert det.observe(1, SimpleNamespace(state="RUNNING", progress=2.0, layer_num=1), 0) is False


class TestScheduler:
    def test_held_printer_is_not_idle(self, monkeypatch):
        from backend.app.services import print_scheduler as ps

        fake = SimpleNamespace(
            is_connected=lambda pid: True,
            get_status=lambda pid: SimpleNamespace(state="IDLE"),
            is_awaiting_plate_clear=lambda pid: False,
        )
        monkeypatch.setattr(ps, "printer_manager", fake)
        sched = ps.PrintScheduler()
        assert sched._is_printer_idle(4, require_plate_clear=False)
        fd.HELD.add(4)
        try:
            assert not sched._is_printer_idle(4, require_plate_clear=False)
        finally:
            fd.HELD.discard(4)


class TestKlipperMessage:
    def test_status_map_keeps_the_error_text(self):
        from backend.app.services.bambu_mqtt import PrinterState
        from backend.app.services.klipper.status_map import apply_status_objects

        state = PrinterState()
        apply_status_objects(state, {"print_stats": {"state": "error", "message": "Heater extruder not heating"}}, None)
        assert state.raw_data.get("klipper_message") == "Heater extruder not heating"


class TestRoutes:
    async def test_policy_events_and_ack(self, async_client, wired):
        r = await async_client.put("/api/v1/failure-detection/policy", json={"hold_printer": True, "stall_minutes": 20})
        assert r.status_code == 200, r.text
        assert (await async_client.get("/api/v1/failure-detection/policy")).json() == {
            "hold_printer": True,
            "stall_minutes": 20,
        }
        await fd.record_event(11, "obico", "spaghetti", score=0.8)
        r = await async_client.get("/api/v1/failure-events?printer_id=11&unacknowledged=true")
        assert r.status_code == 200 and len(r.json()) == 1
        r = await async_client.post("/api/v1/printers/11/failure-events/ack", json={"by": "me"})
        assert r.json() == {"acknowledged": 1, "held": False}
