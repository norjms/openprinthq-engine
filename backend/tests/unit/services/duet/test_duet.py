"""Tests for Duet status mapping + client (no I/O)."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.duet.duet_client import DuetClient
from backend.app.services.duet.status_map import apply_duet_status, map_duet_state
from backend.app.services.printer_client import PrinterClient


def test_state_vocabulary():
    assert map_duet_state("processing") == "RUNNING"
    assert map_duet_state("idle") == "IDLE"
    assert map_duet_state("paused") == "PAUSE"
    assert map_duet_state("halted") == "FAILED"
    assert map_duet_state(None) == "unknown"


def test_object_model_mapping():
    state = PrinterState()
    model = {
        "state": {"status": "processing"},
        "heat": {
            "heaters": [
                {"current": 60.0, "active": 60.0},  # 0 = bed
                {"current": 205.3, "active": 210.0},  # 1 = tool
            ],
            "bedHeaters": [0],
            "chamberHeaters": [-1],
        },
        "tools": [{"heaters": [1]}],
        "job": {
            "file": {"fileName": "cube.gcode", "size": 1000},
            "filePosition": 250,
            "timesLeft": {"file": 1200},
            "duration": 300,
        },
    }
    mapped = apply_duet_status(state, model)
    assert mapped == "RUNNING"
    assert state.temperatures["bed"] == 60.0
    assert state.temperatures["nozzle"] == 205.3
    assert state.temperatures["nozzle_target"] == 210.0
    assert state.progress == 25.0  # 250/1000
    assert state.remaining_time == 20  # 1200s
    assert state.gcode_file == "cube.gcode"
    assert state.raw_data["print_duration"] == 300.0


def test_falls_back_to_heater_indices_without_tools():
    state = PrinterState()
    model = {
        "state": {"status": "idle"},
        "heat": {"heaters": [{"current": 25.0, "active": 0}, {"current": 24.0, "active": 0}]},
        "job": {},
    }
    apply_duet_status(state, model)
    assert state.temperatures["bed"] == 25.0
    assert state.temperatures["nozzle"] == 24.0


def test_satisfies_protocol():
    assert isinstance(DuetClient("10.0.0.7"), PrinterClient)


def test_default_password_and_control_returns_false_without_loop():
    c = DuetClient("10.0.0.7")
    assert c.password == "reprap"  # RRF default
    # No loop → control can't schedule, returns False.
    assert c.pause_print() is False
    assert c.start_print("x.gcode") is False
    assert c.set_bed_temp(60) is False
