"""H2-series nozzle material in the hardware match (#33).

H2D and H2C report the mounted hotends in ``device.nozzle.info[]`` with a compact
type code (``HS01``, ``HH01``), not the long names X1/P1 firmware sends in
``nozzle_type``. The farm matcher, the pre-send check and the dispatcher must
decode it the same way the nozzle view does, so a printer with hardened-steel
hotends is not held for abrasive filament as if its nozzles were brass.
"""

from __future__ import annotations

import pytest

from backend.app.models.printer import Printer
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.farm_match import hardware_mismatch, installed_nozzles
from backend.app.services.print_scheduler import PrintScheduler

ABRASIVE_JOB = {"nozzle_diameter": 0.4, "bed_type": None, "abrasive": ["PA-CF"]}


def _h2_entry(nozzle_id: int, code: str, diameter: float = 0.4) -> dict:
    """One ``device.nozzle.info[]`` entry as an H2C sends it (H2D uses the long key names)."""
    return {
        "id": nozzle_id,
        "type": code,
        "diameter": diameter,
        "wear": 3,
        "stat": 1 if nozzle_id < 2 else 0,
        "tm": 300,
        "sn": f"SN-{nozzle_id}",
        "color_m": "00000000",
        "fila_id": "",
    }


def _h2_state(*codes: str, rack: tuple[tuple[int, str], ...] = ()):
    """PrinterState after an H2 push with the given mounted hotend codes (index = extruder)."""
    client = BambuMQTTClient(ip_address="192.0.2.33", serial_number="TESTH2NOZZLE", access_code="12345678")
    info = [_h2_entry(i, code) for i, code in enumerate(codes)]
    info += [_h2_entry(rack_id, code) for rack_id, code in rack]
    client._process_message({"print": {"device": {"nozzle": {"exist": 3, "info": info}}}})
    return client.state


def _legacy_state(nozzle_type: str):
    """PrinterState after an X1/P1 push that names the material in ``nozzle_type``."""
    client = BambuMQTTClient(ip_address="192.0.2.34", serial_number="TESTX1NOZZLE", access_code="12345678")
    client._process_message({"print": {"nozzle_type": nozzle_type, "nozzle_diameter": "0.4"}})
    return client.state


class TestInstalledNozzles:
    def test_h2c_both_hardened(self):
        state = _h2_state("HS01", "HS01", rack=((16, "HH00"), (17, "HS00")))
        assert installed_nozzles(state) == [(0.4, "hardened_steel"), (0.4, "hardened_steel")]
        assert hardware_mismatch(ABRASIVE_JOB, state, None) == []

    def test_h2d_high_flow_hardened(self):
        state = _h2_state("HH01", "HH01")
        assert installed_nozzles(state) == [(0.4, "hardened_steel"), (0.4, "hardened_steel")]
        assert hardware_mismatch(ABRASIVE_JOB, state, None) == []

    def test_lowercase_code_decodes_too(self):
        state = _h2_state("hs01", "hh05")
        assert installed_nozzles(state) == [(0.4, "hardened_steel"), (0.4, "tungsten_carbide")]

    def test_one_hardened_one_stainless_passes(self):
        # Either hotend can take the abrasive filament, as with the diameter check.
        state = _h2_state("HS00", "HS01")
        assert installed_nozzles(state) == [(0.4, "stainless_steel"), (0.4, "hardened_steel")]
        assert hardware_mismatch(ABRASIVE_JOB, state, None) == []

    def test_tungsten_carbide_passes(self):
        assert hardware_mismatch(ABRASIVE_JOB, _h2_state("HS05", "HS05"), None) == []

    def test_stainless_only_is_held(self):
        reasons = hardware_mismatch(ABRASIVE_JOB, _h2_state("HS00", "HH00"), None)
        assert reasons == ["a hardened nozzle for PA-CF (has stainless_steel)"]

    def test_unknown_codes_are_unknown_and_never_block(self):
        # An unknown material digit, a code from another tool head (cutter), the bare flow
        # family and N/A: the material is unknown, which never holds a job.
        state = _h2_state("HS09", "CP00")
        assert installed_nozzles(state) == [(0.4, ""), (0.4, "")]
        assert hardware_mismatch(ABRASIVE_JOB, state, None) == []
        assert installed_nozzles(_h2_state("HS", "N/A")) == [(0.4, ""), (0.4, "")]

    def test_non_abrasive_job_is_unaffected(self):
        assert hardware_mismatch({"nozzle_diameter": 0.4, "abrasive": []}, _h2_state("HS00", "HS00"), None) == []

    @pytest.mark.parametrize(
        ("nozzle_type", "material", "held"),
        [
            ("hardened_steel", "hardened_steel", False),
            ("stainless_steel", "stainless_steel", True),
            ("brass", "brass", True),
            ("tungsten_carbide", "tungsten_carbide", False),
        ],
    )
    def test_x1_p1_long_names_unchanged(self, nozzle_type, material, held):
        state = _legacy_state(nozzle_type)
        assert installed_nozzles(state)[0] == (0.4, material)
        assert bool(hardware_mismatch(ABRASIVE_JOB, state, None)) is held

    def test_manual_override_wins_over_the_h2_report(self):
        # Hardened hotends reported, but someone says extruder 0 now has a brass nozzle and
        # extruder 1 is unknown: brass is all that is known, so the job is held.
        state = _h2_state("HS01", "HS01")
        manual = {0: (0.4, "brass"), 1: (0.4, "undefine")}
        assert installed_nozzles(state, manual) == [(0.4, "brass"), (0.4, "")]
        assert hardware_mismatch(ABRASIVE_JOB, state, None, manual) == ["a hardened nozzle for PA-CF (has brass)"]
        # And the other way round: stainless reported, hardened set by hand.
        state = _h2_state("HS00", "HS00")
        assert hardware_mismatch(ABRASIVE_JOB, state, None, {1: (0.4, "hardened_steel")}) == []


class _FakeManager:
    def __init__(self, statuses):
        self.statuses = statuses

    def is_connected(self, pid):
        return True

    def get_status(self, pid):
        return self.statuses.get(pid)

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name, model):
    p = Printer(name=name, serial_number=f"h2-{name}", ip_address="192.0.2.35", model=model, access_code="00000000")
    db.add(p)
    await db.flush()
    return p


class TestDispatch:
    async def test_h2c_with_hardened_hotends_takes_an_abrasive_job(self, db_session, monkeypatch):
        hardened = await _printer(db_session, "H2C-hardened", "H2C")
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _FakeManager({hardened.id: _h2_state("HS01", "HS01")}),
        )
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        pid, reason = await sched._find_idle_printer_for_model(db_session, "H2C", set(), job_hw=ABRASIVE_JOB)
        assert (pid, reason) == (hardened.id, None)

    async def test_h2d_with_stainless_hotends_is_held(self, db_session, monkeypatch):
        stainless = await _printer(db_session, "H2D-stainless", "H2D")
        await db_session.commit()
        sched = PrintScheduler()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _FakeManager({stainless.id: _h2_state("HS00", "HH00")}),
        )
        monkeypatch.setattr(sched, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        pid, reason = await sched._find_idle_printer_for_model(db_session, "H2D", set(), job_hw=ABRASIVE_JOB)
        assert pid is None
        assert "H2D-stainless (needs a hardened nozzle for PA-CF (has stainless_steel))" in reason


class TestPreflight:
    async def test_preflight_answers_from_the_decoded_h2_material(self, async_client, db_session, monkeypatch):
        from backend.app.services import printer_manager as pm_module
        from backend.app.services.print_scheduler import scheduler
        from backend.app.services.printer_hardware import set_manual_nozzle

        h2c = await _printer(db_session, "H2C", "H2C")
        h2d = await _printer(db_session, "H2D", "H2D")
        await db_session.commit()
        monkeypatch.setattr(
            pm_module,
            "printer_manager",
            _FakeManager({h2c.id: _h2_state("HS01", "HS01"), h2d.id: _h2_state("HS00", "HS01")}),
        )
        monkeypatch.setattr(scheduler, "_is_printer_idle", lambda pid, require_plate_clear=True: True)

        async def check(pid):
            r = await async_client.post(
                "/api/v1/queue/preflight",
                json={"printer_id": pid, "filament_types": ["PA-CF"], "skip_filament_check": True},
            )
            assert r.status_code == 200, r.text
            assert r.json()["needs"]["abrasive"] == ["PA-CF"]
            return r.json()["printers"][0]

        for pid in (h2c.id, h2d.id):
            row = await check(pid)
            assert row["eligible"] is True and row["reasons"] == [], row

        # A manual override still wins for its extruder: brass on both hotends holds the job.
        await set_manual_nozzle(db_session, h2c.id, 0, 0.4, "brass", "Standard")
        await set_manual_nozzle(db_session, h2c.id, 1, 0.4, "brass", "Standard")
        row = await check(h2c.id)
        assert row["eligible"] is False
        assert row["reasons"] == ["needs a hardened nozzle for PA-CF (has brass)"]
