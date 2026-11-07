"""Unified alarm feed: what counts as an alarm per transport, the diff against open rows, routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.printer_alarm import PrinterAlarm
from backend.app.services import alarm_feed as af
from backend.app.services.octoprint.octoprint_client import PrusaLinkClient


def hms(short: str, severity: int):
    module, code = short.split("_")
    return SimpleNamespace(attr=int(module, 16) << 16, code=f"0x{code}", severity=severity, message="")


def st(**kw):
    base = {"connected": True, "state": "IDLE", "hms_errors": [], "raw_data": {}}
    base.update(kw)
    return SimpleNamespace(**base)


class Bambu:
    """Stands in for BambuMQTTClient (source_of goes by class name)."""


Bambu.__name__ = "BambuMQTTClient"


class TestActiveAlarms:
    def test_bambu_hms_by_severity_skipping_info_and_cancel(self):
        alarms = af.active_alarms(
            st(hms_errors=[hms("0700_8010", 2), hms("0300_8003", 1), hms("0C00_0001", 4), hms("0500_400E", 2)]),
            "bambu",
        )
        got = {(a.code, a.severity) for a in alarms}
        assert got == {("0700_8010", "serious"), ("0300_8003", "fatal")}
        ams = next(a for a in alarms if a.code == "0700_8010")
        assert "AMS assist motor" in ams.message

    def test_bambu_duplicate_code_keeps_the_worst(self):
        alarms = af.active_alarms(st(hms_errors=[hms("0700_8010", 3), hms("0700_8010", 1)]), "bambu")
        assert [(a.code, a.severity) for a in alarms] == [("0700_8010", "fatal")]

    def test_bambu_failed_print_state_alone_is_not_an_alarm(self):
        assert af.active_alarms(st(state="FAILED"), "bambu") == []

    def test_klipper(self):
        assert af.active_alarms(st(state="FAILED", raw_data={"klipper_message": ""}), "klipper") == []  # cancelled
        [a] = af.active_alarms(st(state="FAILED", raw_data={"klipper_message": "MCU 'mcu' shutdown"}), "klipper")
        assert (a.code, a.severity, a.message) == ("print_error", "serious", "MCU 'mcu' shutdown")
        [a] = af.active_alarms(st(state="unknown", raw_data={"klippy_alarm": "shutdown"}), "klipper")
        assert (a.code, a.severity) == ("klippy_shutdown", "fatal")

    def test_prusalink(self):
        [a] = af.active_alarms(st(state="PAUSE", raw_data={"printer_state_text": "ATTENTION"}), "prusalink")
        assert (a.code, a.severity) == ("attention", "warning")
        [a] = af.active_alarms(st(state="FAILED", raw_data={"printer_state_text": "ERROR"}), "prusalink")
        assert a.code == "printer_error"
        assert af.active_alarms(st(state="RUNNING", raw_data={"printer_state_text": "PRINTING"}), "prusalink") == []

    @pytest.mark.parametrize("source", ["octoprint", "duet", "flashforge", "mks", "snapmaker"])
    def test_error_state_on_other_transports(self, source):
        [a] = af.active_alarms(st(state="FAILED"), source)
        assert (a.source, a.code) == (source, "printer_error")
        assert af.active_alarms(st(state="RUNNING"), source) == []

    def test_unknown_and_obico(self):
        assert af.active_alarms(st(state="FAILED"), None) == []
        assert af.active_alarms(st(state="FAILED"), "obico") == []

    def test_source_of(self):
        assert af.source_of(Bambu()) == "bambu"
        assert af.source_of(PrusaLinkClient("192.0.2.5")) == "prusalink"
        assert af.source_of(None) is None
        assert af.source_of(object()) is None


@pytest.fixture
def wired(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(af.database, "async_session", maker)
    notified = []

    async def fake_notify(printer_id, source, new):
        notified.append((printer_id, source, [a.code for a in new]))

    monkeypatch.setattr(af, "_notify", fake_notify)
    af.reset()
    clock = [1000.0]
    monkeypatch.setattr(af, "_clock", lambda: clock[0])
    yield SimpleNamespace(maker=maker, notified=notified, clock=clock)
    af.reset()


async def _rows(maker):
    async with maker() as db:
        return (await db.execute(select(PrinterAlarm).order_by(PrinterAlarm.id))).scalars().all()


class TestObserve:
    async def test_new_alarm_is_recorded_once_and_cleared_after_grace(self, wired):
        bambu = Bambu()
        on = st(hms_errors=[hms("0700_8010", 2)])
        new = await af.observe(1, on, bambu)
        assert [a.code for a in new] == ["0700_8010"]
        assert await af.observe(1, on, bambu) == []  # still active: no new row
        rows = await _rows(wired.maker)
        assert len(rows) == 1 and rows[0].cleared_at is None and rows[0].severity == "serious"

        off = st()
        await af.observe(1, off, bambu)
        wired.clock[0] += af.CLEAR_GRACE_S - 1
        await af.observe(1, off, bambu)
        assert (await _rows(wired.maker))[0].cleared_at is None  # still within the grace
        wired.clock[0] += 2
        await af.observe(1, off, bambu)
        assert (await _rows(wired.maker))[0].cleared_at is not None

    async def test_flapping_code_stays_one_row(self, wired):
        bambu = Bambu()
        on, off = st(hms_errors=[hms("0700_8010", 3)]), st()
        for _ in range(5):
            await af.observe(2, on, bambu)
            wired.clock[0] += 5
            await af.observe(2, off, bambu)
            wired.clock[0] += 5
        assert len(await _rows(wired.maker)) == 1

    async def test_disconnect_does_not_clear(self, wired):
        bambu = Bambu()
        await af.observe(3, st(hms_errors=[hms("0700_8010", 2)]), bambu)
        wired.clock[0] += 600
        await af.observe(3, st(connected=False), bambu)
        assert (await _rows(wired.maker))[0].cleared_at is None

    async def test_open_rows_reload_after_restart(self, wired):
        bambu = Bambu()
        on = st(hms_errors=[hms("0700_8010", 2)])
        await af.observe(4, on, bambu)
        af.reset()  # engine restart: cache gone, rows stay
        assert await af.observe(4, on, bambu) == []
        assert len(await _rows(wired.maker)) == 1

    async def test_notifies_external_transports_only_through_notify(self, wired):
        client = PrusaLinkClient("192.0.2.5")
        await af.observe(5, st(state="PAUSE", raw_data={"printer_state_text": "ATTENTION"}), client)
        assert wired.notified == [(5, "prusalink", ["attention"])]

    async def test_acknowledge(self, wired):
        bambu = Bambu()
        await af.observe(6, st(hms_errors=[hms("0700_8010", 2), hms("0300_8003", 1)]), bambu)
        rows = await _rows(wired.maker)
        assert await af.acknowledge([rows[0].id], None, "op") == 1
        assert await af.acknowledge(None, 6, "op") == 1
        assert all(r.acknowledged_by == "op" for r in await _rows(wired.maker))


class TestNotifyRules:
    async def test_bambu_never_notifies_from_the_feed(self, monkeypatch):
        called = []
        monkeypatch.setattr(af.database, "async_session", lambda: called.append(1))
        await af._notify(1, "bambu", [af.Alarm("bambu", "0700_8010", "fatal", "x")])
        assert called == []

    async def test_warning_without_attention_is_quiet(self, monkeypatch):
        called = []
        monkeypatch.setattr(af.database, "async_session", lambda: called.append(1))
        await af._notify(1, "octoprint", [af.Alarm("octoprint", "something", "warning", "x")])
        assert called == []


class TestRoutes:
    async def test_list_summary_and_ack(self, async_client, wired):
        bambu = Bambu()
        await af.observe(11, st(hms_errors=[hms("0700_8010", 3), hms("0300_8003", 1)]), bambu)
        r = await async_client.get("/api/v1/alarms")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [a["severity"] for a in body] == ["fatal", "warning"]  # worst first
        assert all(a["active"] for a in body)
        r = await async_client.get("/api/v1/alarms?severity=warning")
        assert [a["code"] for a in r.json()] == ["0700_8010"]
        s = (await async_client.get("/api/v1/alarms/summary")).json()
        assert s["active"] == 2 and s["unacknowledged"] == 2 and s["printers"] == [11]
        assert s["by_severity"]["fatal"] == 1
        aid = body[0]["id"]
        r = await async_client.post(f"/api/v1/alarms/{aid}/ack", json={"by": "me"})
        assert r.json() == {"acknowledged": 1}
        r = await async_client.post("/api/v1/printers/11/alarms/ack")
        assert r.json() == {"acknowledged": 1}
        assert (await async_client.get("/api/v1/alarms/summary")).json()["unacknowledged"] == 0
        assert (await async_client.get(f"/api/v1/alarms/{aid}")).json()["acknowledged_by"] == "me"
        assert (await async_client.post("/api/v1/alarms/999999/ack")).status_code == 404
        assert (await async_client.get("/api/v1/alarms?state=cleared")).json() == []
