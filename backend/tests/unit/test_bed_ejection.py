"""Server-side bed ejection: eligibility, the run, the gate, and the routes.

The printer client is always a fake; no test sends anything to hardware.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.ejection_profile import EjectionProfile
from backend.app.models.printer import Printer
from backend.app.services import bed_ejection


class _FakeClient:
    def __init__(self, ok=True):
        self.sent: list[str] = []
        self.ok = ok
        self.state = SimpleNamespace(connected=True)

    def send_gcode(self, g):
        self.sent.append(g)
        return self.ok


class _FakeManager:
    def __init__(self, client, bed=30.0, state="FINISH"):
        self.client = client
        self.bed = bed
        self.state = state
        self.gate: dict[int, bool] = {}

    def get_status(self, pid):
        return SimpleNamespace(state=self.state, temperatures={"bed": self.bed})

    def get_client(self, pid):
        return self.client

    def is_connected(self, pid):
        return True

    def set_awaiting_plate_clear(self, pid, v):
        self.gate[pid] = v

    def is_awaiting_plate_clear(self, pid):
        return self.gate.get(pid, False)


async def _no_sleep(_s):
    return None


@pytest.fixture(autouse=True)
def _clean_eject_state():
    bed_ejection.EJECTING.clear()
    bed_ejection.LAST_RESULT.clear()
    yield
    bed_ejection.EJECTING.clear()
    bed_ejection.LAST_RESULT.clear()


@pytest.fixture
def wired(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(bed_ejection, "async_session", maker)
    bed_ejection.EJECTING.clear()
    bed_ejection.LAST_RESULT.clear()

    def use(manager):
        monkeypatch.setattr(bed_ejection, "printer_manager", manager)
        return manager

    return SimpleNamespace(maker=maker, use=use)


async def _setup(db, **pkw):
    prof = EjectionProfile(
        name="sweep",
        gcode="EJECT_PART",
        max_bed_temp_c=pkw.pop("max_bed_temp_c", 35.0),
        timeout_s=pkw.pop("timeout_s", 60),
        settle_s=0,
        max_consecutive=pkw.pop("max_consecutive", 3),
        allowed_plates=pkw.pop("allowed_plates", None),
        verify_with_camera=pkw.pop("verify_with_camera", False),
    )
    db.add(prof)
    await db.flush()
    p = Printer(
        name="P",
        serial_number="EJ1",
        ip_address="192.0.2.10",
        access_code="x",
        model="X1C",
        auto_eject=pkw.pop("auto_eject", True),
        ejection_profile_id=prof.id,
        installed_plate=pkw.pop("installed_plate", "Textured PEI Plate"),
        consecutive_ejects=pkw.pop("consecutive_ejects", 0),
    )
    db.add(p)
    await db.commit()
    return p, prof


class TestEligibility:
    def _p(self, **kw):
        base = {"auto_eject": True, "installed_plate": "Textured PEI Plate", "consecutive_ejects": 0}
        base.update(kw)
        return SimpleNamespace(**base)

    def _prof(self, **kw):
        base = {"allowed_plates": None, "max_consecutive": 3}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_only_completed_prints(self):
        assert bed_ejection.eligibility(self._p(), self._prof(), "completed") is None
        for st in ("failed", "aborted", "cancelled"):
            assert "only completed" in bed_ejection.eligibility(self._p(), self._prof(), st)

    def test_off_or_no_profile(self):
        assert "off" in bed_ejection.eligibility(self._p(auto_eject=False), self._prof(), "completed")
        assert "no ejection profile" in bed_ejection.eligibility(self._p(), None, "completed")

    def test_plates_compare_loosely(self):
        prof = self._prof(allowed_plates=["textured pei"])
        assert bed_ejection.eligibility(self._p(), prof, "completed") is None
        assert "not one the profile allows" in bed_ejection.eligibility(
            self._p(installed_plate="Cool Plate"), prof, "completed"
        )
        assert "no installed plate" in bed_ejection.eligibility(self._p(installed_plate=None), prof, "completed")

    def test_consecutive_cap(self):
        assert "ejects in a row" in bed_ejection.eligibility(self._p(consecutive_ejects=3), self._prof(), "completed")
        assert (
            bed_ejection.eligibility(self._p(consecutive_ejects=99), self._prof(max_consecutive=0), "completed") is None
        )


class TestRun:
    async def test_success_clears_gate_and_counts(self, wired, db_session):
        p, prof = await _setup(db_session)
        m = wired.use(_FakeManager(_FakeClient()))
        m.gate[p.id] = True
        bed_ejection.EJECTING.add(p.id)
        res = await bed_ejection.run_eject(p.id, prof.id, sleep=_no_sleep)
        assert res.ok, res.reason
        assert m.client.sent == ["EJECT_PART"]
        assert m.gate[p.id] is False
        assert p.id not in bed_ejection.EJECTING
        async with wired.maker() as s:
            assert (await s.get(Printer, p.id)).consecutive_ejects == 1

    async def test_hot_bed_times_out_and_keeps_gate(self, wired, db_session):
        p, prof = await _setup(db_session, max_bed_temp_c=35.0, timeout_s=60)
        m = wired.use(_FakeManager(_FakeClient(), bed=60.0))
        m.gate[p.id] = True
        clock = iter(range(0, 10_000, 10))
        res = await bed_ejection.run_eject(p.id, prof.id, sleep=_no_sleep, clock=lambda: next(clock))
        assert not res.ok and "did not cool" in res.reason
        assert m.client.sent == [] and m.gate[p.id] is True

    async def test_refused_gcode_keeps_gate(self, wired, db_session):
        p, prof = await _setup(db_session)
        m = wired.use(_FakeManager(_FakeClient(ok=False)))
        m.gate[p.id] = True
        res = await bed_ejection.run_eject(p.id, prof.id, sleep=_no_sleep)
        assert not res.ok and "refused" in res.reason and m.gate[p.id] is True

    async def test_never_idle_again_keeps_gate(self, wired, db_session):
        p, prof = await _setup(db_session)
        m = wired.use(_FakeManager(_FakeClient(), state="RUNNING"))
        m.gate[p.id] = True
        clock = iter(range(0, 10_000, 10))
        res = await bed_ejection.run_eject(p.id, prof.id, sleep=_no_sleep, clock=lambda: next(clock))
        assert not res.ok and "idle" in res.reason and m.gate[p.id] is True

    async def test_camera_veto_keeps_gate(self, wired, db_session, monkeypatch):
        p, prof = await _setup(db_session, verify_with_camera=True)
        m = wired.use(_FakeManager(_FakeClient()))
        m.gate[p.id] = True

        async def seen(_printer):
            return False, "camera still sees something on the bed"

        monkeypatch.setattr(bed_ejection, "_verify_empty", seen)
        res = await bed_ejection.run_eject(p.id, prof.id, sleep=_no_sleep)
        assert not res.ok and "camera" in res.reason and m.gate[p.id] is True

    async def test_after_print_only_when_eligible(self, wired, db_session, monkeypatch):
        p, _prof = await _setup(db_session)
        wired.use(_FakeManager(_FakeClient()))
        started = []
        monkeypatch.setattr(bed_ejection, "start", lambda pid, prof_id: started.append((pid, prof_id)))
        assert "only completed" in await bed_ejection.maybe_start_after_print(p.id, "failed")
        assert p.id not in bed_ejection.EJECTING and started == []
        assert await bed_ejection.maybe_start_after_print(p.id, "completed") is None
        assert started and started[0][0] == p.id


class TestScheduler:
    def test_ejecting_printer_is_never_idle(self, monkeypatch):
        from backend.app.services import print_scheduler as ps

        fake = _FakeManager(_FakeClient(), state="IDLE")
        monkeypatch.setattr(ps, "printer_manager", fake)
        sched = ps.PrintScheduler()
        assert sched._is_printer_idle(7, require_plate_clear=False) is True
        bed_ejection.EJECTING.add(7)
        try:
            assert sched._is_printer_idle(7, require_plate_clear=False) is False
        finally:
            bed_ejection.EJECTING.discard(7)


class TestRoutes:
    async def test_profile_crud_and_printer_fields(self, async_client, db_session):
        r = await async_client.post(
            "/api/v1/ejection-profiles",
            json={"name": "Voron sweep", "gcode": "EJECT", "max_bed_temp_c": 30, "allowed_plates": ["Textured PEI"]},
        )
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = await async_client.patch(f"/api/v1/ejection-profiles/{pid}", json={"max_consecutive": 2})
        assert r.status_code == 200 and r.json()["max_consecutive"] == 2
        assert any(x["id"] == pid for x in (await async_client.get("/api/v1/ejection-profiles")).json())

        printer = Printer(name="R", serial_number="EJR", ip_address="192.0.2.11", access_code="x", model="P1S")
        db_session.add(printer)
        await db_session.commit()
        r = await async_client.patch(
            f"/api/v1/printers/{printer.id}", json={"auto_eject": True, "ejection_profile_id": pid}
        )
        assert r.status_code == 200, r.text
        assert r.json()["auto_eject"] is True and r.json()["ejection_profile_id"] == pid

        r = await async_client.get(f"/api/v1/printers/{printer.id}/eject")
        assert r.status_code == 200 and r.json()["ejecting"] is False

        r = await async_client.delete(f"/api/v1/ejection-profiles/{pid}")
        assert r.status_code == 200
        r = await async_client.get(f"/api/v1/printers/{printer.id}/eject")
        assert r.json()["ejection_profile_id"] is None

    async def test_manual_eject_needs_profile(self, async_client, db_session):
        printer = Printer(name="M", serial_number="EJM", ip_address="192.0.2.12", access_code="x", model="P1S")
        db_session.add(printer)
        await db_session.commit()
        r = await async_client.post(f"/api/v1/printers/{printer.id}/eject")
        assert r.status_code == 400
