"""Tests for FlashForge status parsing + client (no I/O)."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.flashforge.flashforge_client import FlashForgeClient
from backend.app.services.flashforge.status_map import (
    apply_machine_status,
    apply_progress,
    apply_temps,
    map_ff_state,
)
from backend.app.services.printer_client import PrinterClient


def test_state_vocabulary():
    assert map_ff_state("BUILDING_FROM_SD") == "RUNNING"
    assert map_ff_state("READY") == "IDLE"
    assert map_ff_state("PAUSED") == "PAUSE"
    assert map_ff_state("ERROR") == "FAILED"
    assert map_ff_state(None) == "unknown"


def test_apply_temps():
    state = PrinterState()
    apply_temps(state, "CMD M105 Received.\nT0:205 /210 B:59.5/60\nok")
    assert state.temperatures["nozzle"] == 205.0
    assert state.temperatures["nozzle_target"] == 210.0
    assert state.temperatures["bed"] == 59.5
    assert state.temperatures["bed_target"] == 60.0


def test_apply_progress():
    state = PrinterState()
    apply_progress(state, "CMD M27 Received.\nSD printing byte 2500/10000\nok")
    assert state.progress == 25.0


def test_apply_machine_status():
    state = PrinterState()
    mapped = apply_machine_status(
        state,
        "CMD M119 Received.\nEndstop: X-max:0 Y-max:0 Z-max:0\nMachineStatus: BUILDING_FROM_SD\n"
        "CurrentFile: cube.gcode\nok",
    )
    assert mapped == "RUNNING"
    assert state.gcode_file == "cube.gcode"
    assert state.current_print == "cube.gcode"


def test_satisfies_protocol():
    assert isinstance(FlashForgeClient("10.0.0.8"), PrinterClient)


def test_control_returns_false_without_loop():
    c = FlashForgeClient("10.0.0.8")
    assert c.pause_print() is False
    assert c.start_print("x.gcode") is False
    assert c.set_bed_temp(60) is False
