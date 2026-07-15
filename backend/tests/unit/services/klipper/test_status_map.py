"""Tests for the pure Moonraker → PrinterState mapping."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.klipper.status_map import (
    apply_status_objects,
    map_print_state,
)

# The test payloads report chamber under this object name.
PROFILE = "temperature_sensor chamber"


def test_state_vocabulary_mapping():
    assert map_print_state("standby") == "IDLE"
    assert map_print_state("printing") == "RUNNING"
    assert map_print_state("paused") == "PAUSE"
    assert map_print_state("complete") == "FINISH"
    assert map_print_state("cancelled") == "FAILED"
    assert map_print_state("error") == "FAILED"
    assert map_print_state(None) == "unknown"
    assert map_print_state("weird_new_state") == "unknown"


def test_full_printing_snapshot_maps_core_fields():
    state = PrinterState()
    objects = {
        "print_stats": {
            "state": "printing",
            "filename": "benchy.gcode",
            "print_duration": 600.0,  # 10 min elapsed
            "info": {"current_layer": 12, "total_layer": 120},
        },
        "display_status": {"progress": 0.25},  # 25%
        "extruder": {"temperature": 248.3, "target": 250.0},
        "heater_bed": {"temperature": 99.8, "target": 100.0},
        "temperature_sensor chamber": {"temperature": 45.6},
        "fan": {"speed": 0.5},
    }

    raw = apply_status_objects(state, objects, PROFILE)

    assert raw == "printing"
    assert state.state == "RUNNING"
    assert state.gcode_file == "benchy.gcode"
    assert state.current_print == "benchy.gcode"
    assert state.progress == 25.0
    assert state.layer_num == 12
    assert state.total_layers == 120
    assert state.temperatures["nozzle"] == 248.3
    assert state.temperatures["nozzle_target"] == 250.0
    assert state.temperatures["bed"] == 99.8
    assert state.temperatures["bed_target"] == 100.0
    assert state.temperatures["chamber"] == 45.6
    assert state.cooling_fan_speed == 50
    # remaining = 600 * (1/0.25 - 1) = 1800s = 30 min
    assert state.remaining_time == 30


def test_incremental_update_preserves_prior_fields():
    state = PrinterState()
    apply_status_objects(
        state,
        {
            "print_stats": {"state": "printing", "filename": "x.gcode"},
            "heater_bed": {"temperature": 60.0, "target": 60.0},
        },
        PROFILE,
    )
    # A later update touching only the extruder must not wipe bed/filename/state.
    raw = apply_status_objects(state, {"extruder": {"temperature": 200.0}}, PROFILE)

    assert raw is None  # print_stats absent from this update
    assert state.gcode_file == "x.gcode"
    assert state.state == "RUNNING"
    assert state.temperatures["bed"] == 60.0
    assert state.temperatures["nozzle"] == 200.0


def test_progress_falls_back_to_virtual_sdcard():
    state = PrinterState()
    apply_status_objects(state, {"virtual_sdcard": {"progress": 0.4}}, PROFILE)
    assert state.progress == 40.0


def test_display_status_progress_takes_precedence_over_sdcard():
    state = PrinterState()
    apply_status_objects(
        state,
        {"display_status": {"progress": 0.6}, "virtual_sdcard": {"progress": 0.4}},
        PROFILE,
    )
    assert state.progress == 60.0


def test_chamber_maps_under_any_resolved_object_name():
    # Real Voron configs vary: this one exposes chamber as a temperature_fan.
    state = PrinterState()
    apply_status_objects(
        state,
        {"temperature_fan chamber": {"temperature": 41.2, "target": 45.0}},
        "temperature_fan chamber",
    )
    assert state.temperatures["chamber"] == 41.2
    assert state.temperatures["chamber_target"] == 45.0


def test_chamber_skipped_when_no_object_resolved():
    state = PrinterState()
    apply_status_objects(state, {"temperature_fan chamber": {"temperature": 41.2}}, None)
    assert "chamber" not in state.temperatures


def test_idle_state_no_remaining_time():
    state = PrinterState()
    apply_status_objects(
        state,
        {"print_stats": {"state": "standby", "filename": "", "print_duration": 0.0}},
        PROFILE,
    )
    assert state.state == "IDLE"
    assert state.gcode_file is None
    assert state.remaining_time == 0
