"""Tests for OctoPrint status mapping + client transitions (no I/O)."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.octoprint.octoprint_client import OctoPrintClient, PrusaLinkClient
from backend.app.services.octoprint.status_map import apply_octoprint_status, map_octoprint_state
from backend.app.services.printer_client import PrinterClient


def test_state_vocabulary_mapping():
    assert map_octoprint_state({"printing": True}) == "RUNNING"
    assert map_octoprint_state({"paused": True}) == "PAUSE"
    assert map_octoprint_state({"error": True}) == "FAILED"
    assert map_octoprint_state({"operational": True}) == "IDLE"
    assert map_octoprint_state({}) == "unknown"


def test_apply_full_printing_snapshot():
    state = PrinterState()
    printer = {
        "state": {"flags": {"printing": True, "operational": True}},
        "temperature": {
            "tool0": {"actual": 210.5, "target": 215.0},
            "bed": {"actual": 60.1, "target": 60.0},
        },
    }
    job = {
        "job": {"file": {"name": "benchy.gcode"}},
        "progress": {"completion": 42.7, "printTimeLeft": 1800, "printTime": 600},
    }
    mapped = apply_octoprint_status(state, printer, job)
    assert mapped == "RUNNING"
    assert state.state == "RUNNING"
    assert state.temperatures["nozzle"] == 210.5
    assert state.temperatures["nozzle_target"] == 215.0
    assert state.temperatures["bed"] == 60.1
    assert state.progress == 42.7
    assert state.remaining_time == 30  # 1800s
    assert state.gcode_file == "benchy.gcode"
    assert state.raw_data["print_duration"] == 600.0


def test_octoprint_satisfies_protocol():
    assert isinstance(OctoPrintClient("10.0.0.9"), PrinterClient)
    assert isinstance(PrusaLinkClient("10.0.0.9"), PrinterClient)


def _client(cls=OctoPrintClient):
    events = {"start": [], "complete": []}
    c = cls(
        "10.0.0.9",
        on_print_start=lambda d: events["start"].append(d),
        on_print_complete=lambda d: events["complete"].append(d),
    )
    return c, events


def test_completion_inferred_from_progress():
    c, ev = _client()
    # Start printing
    c._detect_transitions("RUNNING")
    assert len(ev["start"]) == 1
    # Reach ~100% then go back to IDLE -> completed
    c.state.progress = 100.0
    c._detect_transitions("IDLE")
    assert ev["complete"][0]["status"] == "completed"


def test_early_idle_is_cancelled():
    c, ev = _client()
    c._detect_transitions("RUNNING")
    c.state.progress = 30.0
    c._detect_transitions("IDLE")
    assert ev["complete"][0]["status"] == "cancelled"


def test_error_is_failed():
    c, ev = _client()
    c._detect_transitions("RUNNING")
    c._detect_transitions("FAILED")
    assert ev["complete"][0]["status"] == "failed"


def test_prusalink_status_translation():
    c, _ = _client(PrusaLinkClient)
    assert c.flavor == "prusalink"
    # The PrusaLink base URL uses the configured port.
    assert c.base_url.startswith("http://10.0.0.9")
