"""Tests for MoonrakerClient transition detection + control surface.

These exercise the synchronous status-application and callback logic without
opening a websocket (no event loop / network needed).
"""

from backend.app.services.klipper.moonraker_client import MoonrakerClient
from backend.app.services.printer_client import PrinterClient


def _client():
    events = {"start": [], "complete": [], "running": [], "layer": [], "bed": []}
    c = MoonrakerClient(
        ip_address="10.0.0.5",
        model="voron_2.4_350",
        serial_number="klipper:test",
        on_print_start=lambda d: events["start"].append(d),
        on_print_complete=lambda d: events["complete"].append(d),
        on_print_running_observed=lambda d: events["running"].append(d),
        on_layer_change=lambda n: events["layer"].append(n),
        on_bed_temp_update=lambda t: events["bed"].append(t),
    )
    return c, events


def test_satisfies_printer_client_protocol():
    c, _ = _client()
    assert isinstance(c, PrinterClient)


def test_print_start_transition_fires_callbacks():
    c, events = _client()
    # standby → printing
    c._apply_and_signal({"print_stats": {"state": "standby", "filename": ""}})
    c._apply_and_signal({"print_stats": {"state": "printing", "filename": "cube.gcode"}})
    assert len(events["start"]) == 1
    assert len(events["running"]) == 1
    assert events["start"][0]["filename"] == "cube.gcode"
    assert not events["complete"]


def test_print_complete_transition_includes_status_and_duration():
    c, events = _client()
    c._apply_and_signal({"print_stats": {"state": "printing", "filename": "cube.gcode", "print_duration": 100.0}})
    c._apply_and_signal({"print_stats": {"state": "complete", "filename": "cube.gcode", "print_duration": 3600.0}})
    assert len(events["complete"]) == 1
    done = events["complete"][0]
    assert done["status"] == "completed"
    assert done["print_duration"] == 3600.0
    assert done["filename"] == "cube.gcode"


def test_cancelled_maps_to_cancelled_status():
    c, events = _client()
    c._apply_and_signal({"print_stats": {"state": "printing", "filename": "x.gcode"}})
    c._apply_and_signal({"print_stats": {"state": "cancelled", "filename": "x.gcode"}})
    assert events["complete"][0]["status"] == "cancelled"


def test_error_maps_to_failed_status():
    c, events = _client()
    c._apply_and_signal({"print_stats": {"state": "printing", "filename": "x.gcode"}})
    c._apply_and_signal({"print_stats": {"state": "error", "filename": "x.gcode"}})
    assert events["complete"][0]["status"] == "failed"


def test_no_duplicate_start_on_repeated_printing_updates():
    c, events = _client()
    c._apply_and_signal({"print_stats": {"state": "printing", "filename": "x.gcode"}})
    c._apply_and_signal({"print_stats": {"state": "printing"}, "extruder": {"temperature": 210}})
    assert len(events["start"]) == 1  # only the first transition counts


def test_layer_and_bed_callbacks():
    c, events = _client()
    c._apply_and_signal(
        {
            "print_stats": {"state": "printing", "filename": "x.gcode", "info": {"current_layer": 5}},
            "heater_bed": {"temperature": 60.0, "target": 60.0},
        }
    )
    assert events["layer"] == [5]
    assert events["bed"] == [60.0]


def test_control_methods_return_false_when_not_connected():
    c, _ = _client()
    # No event loop / websocket attached → RPCs can't be scheduled, return False.
    assert c.pause_print() is False
    assert c.resume_print() is False
    assert c.stop_print() is False
    assert c.start_print("x.gcode") is False


def test_set_chamber_light_uses_profile_macro():
    c, _ = _client()
    # Voron profile defines light macros, so the call is attempted (returns
    # False only because there's no loop). A profile with no light macro would
    # short-circuit to False before scheduling.
    assert c.set_chamber_light(True) is False  # no loop, but macro exists
