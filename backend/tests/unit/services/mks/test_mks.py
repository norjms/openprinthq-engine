"""Tests for MKS status parsing + client (no I/O)."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.mks.mks_client import MKSClient
from backend.app.services.mks.status_map import apply_progress, apply_temps, map_m27_state
from backend.app.services.printer_client import PrinterClient


def test_state_vocabulary():
    assert map_m27_state("SD printing byte 123/4560") == "RUNNING"
    assert map_m27_state("Not SD printing") == "IDLE"
    assert map_m27_state("SD print paused") == "PAUSE"
    assert map_m27_state(None) == "unknown"


def test_apply_temps():
    state = PrinterState()
    apply_temps(state, "ok T:205.0 /210.0 B:59.5 /60.0 @:127 B@:127")
    assert state.temperatures["nozzle"] == 205.0
    assert state.temperatures["nozzle_target"] == 210.0
    assert state.temperatures["bed"] == 59.5
    assert state.temperatures["bed_target"] == 60.0


def test_apply_progress():
    state = PrinterState()
    mapped = apply_progress(state, "SD printing byte 2500/10000")
    assert mapped == "RUNNING"
    assert state.progress == 25.0


def test_apply_progress_idle():
    state = PrinterState()
    mapped = apply_progress(state, "Not SD printing")
    assert mapped == "IDLE"
    assert state.progress == 0.0


def test_satisfies_protocol():
    assert isinstance(MKSClient("10.0.0.9"), PrinterClient)


def test_control_returns_false_without_loop():
    c = MKSClient("10.0.0.9")
    assert c.pause_print() is False
    assert c.start_print("x.gcode") is False
    assert c.set_bed_temp(60) is False
