"""Elegoo SDCP v3 and Centauri Carbon 2 clients (untested on hardware).

Exercised against the published payloads in backend/tests/_fixtures/elegoo/
without a network: messages are fed to the clients' handlers directly.
"""

import json
from pathlib import Path

import pytest

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.elegoo.cc2_client import ElegooCC2Client
from backend.app.services.elegoo.sdcp_client import ElegooSDCPClient
from backend.app.services.elegoo.status_map import (
    apply_cc2_status,
    apply_sdcp_status,
    canvas_slot_map,
    map_cc2_state,
    map_sdcp_state,
    parse_canvas,
)
from backend.app.services.printer_capabilities import capabilities_for, transport_of
from backend.app.services.printer_client import PrinterClient

FIXTURES = Path(__file__).resolve().parents[3] / "_fixtures" / "elegoo"


def _json(name):
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------- status maps
@pytest.mark.parametrize(
    ("current", "sub", "expected"),
    [
        ([1], 3, "RUNNING"),
        ([1], 6, "PAUSE"),
        ([1], 5, "PAUSE"),
        ([1], 9, "FINISH"),
        ([1], 8, "IDLE"),
        ([1], 16, "PREPARE"),
        ([1], 114, "FAILED"),
        ([0], 0, "IDLE"),
        ([0], 9, "FINISH"),
        ([2, 1], 3, "RUNNING"),  # file transfer in front of printing
        ([9], 0, "IDLE"),  # homing outside a print
        ([], None, "unknown"),
    ],
)
def test_map_sdcp_state(current, sub, expected):
    assert map_sdcp_state(current, sub) == expected


def test_apply_sdcp_status():
    state = PrinterState()
    assert apply_sdcp_status(state, _json("sdcp_status_printing.json")["Status"]) == "RUNNING"
    assert state.temperatures["nozzle"] == 210.5
    assert state.temperatures["bed"] == 60.1
    assert state.temperatures["chamber"] == 31.0
    assert (state.layer_num, state.total_layers) == (150, 500)
    assert state.gcode_file == "test_print.gcode"
    # 60000 / 200000 ticks are seconds below the ms threshold: 30 %, 2333 min left
    assert state.progress == 30.0
    assert state.remaining_time == 2333


def test_sdcp_ticks_in_milliseconds():
    state = PrinterState()
    apply_sdcp_status(
        state, {"CurrentStatus": [1], "PrintInfo": {"Status": 3, "CurrentTicks": 1_800_000, "TotalTicks": 3_600_000}}
    )
    assert state.progress == 50.0
    assert state.remaining_time == 30


@pytest.mark.parametrize(
    ("machine", "ps", "expected"),
    [
        ({"status": 2, "sub_status": 2075}, {"state": "printing"}, "RUNNING"),
        ({"status": 2, "sub_status": 2502}, {}, "PAUSE"),
        ({"status": 2, "sub_status": 2077}, {}, "FINISH"),
        ({"status": 2, "sub_status": 2504}, {}, "IDLE"),
        ({"status": 2, "sub_status": 1405}, {}, "PREPARE"),
        ({"status": 1, "sub_status": 0}, {"state": "complete"}, "FINISH"),
        ({"status": 1, "sub_status": 0}, {"state": "error"}, "FAILED"),
        ({"status": 1, "sub_status": 0}, {"state": "standby"}, "IDLE"),
        ({}, {}, "unknown"),
    ],
)
def test_map_cc2_state(machine, ps, expected):
    assert map_cc2_state(machine, ps) == expected


def test_apply_cc2_full_status():
    state = PrinterState()
    assert apply_cc2_status(state, _json("cc2_full_status.json")["result"]) == "RUNNING"
    assert state.temperatures["nozzle"] == 215.0 and state.temperatures["bed_target"] == 60
    assert state.progress == 45 and state.remaining_time == 73
    assert (state.layer_num, state.total_layers) == (225, 500)
    assert state.cooling_fan_speed == 100
    assert state.gcode_file == "benchy.gcode"


# --------------------------------------------------------------- CANVAS
def test_parse_cc2_canvas():
    (unit,) = parse_canvas(_json("cc2_canvas_status.json")["result"]["canvas_info"])
    assert unit.kind == "canvas" and unit.unit_id == 0 and unit.connected
    assert [s.colour for s in unit.slots] == ["#2850DF", "#FFFFFF", "#F32FF8", "#000000"]
    assert all(s.material == "PLA" for s in unit.slots)
    assert [s.active for s in unit.slots] == [False, False, False, True]
    assert unit.slots[1].name == "PLA Basic" and unit.slots[1].brand == "ELEGOO"
    assert unit.slots[0].nozzle_temp_min == 190


def test_parse_cc1_canvas_placeholders():
    inner = _json("sdcp_canvas_response.json")["Data"]["Data"]
    (unit,) = parse_canvas(inner)
    s = {x.index: x for x in unit.slots}
    assert s[1].active and s[1].material == "PLA"
    assert s[2].present is False and s[2].material is None and s[2].brand is None
    assert s[3].brand is None and s[3].name is None and s[3].colour == "#D32F2F"


def test_canvas_slot_map_carries_complete_entries():
    assert canvas_slot_map([3, -1, 6]) == [
        {"t": 0, "canvas_id": 0, "tray_id": 3},
        {"t": 2, "canvas_id": 1, "tray_id": 2},
    ]
    assert canvas_slot_map([]) == []


# --------------------------------------------------------------- SDCP client
def _sdcp():
    events = {"start": [], "complete": []}
    c = ElegooSDCPClient(
        ip_address="192.0.2.10",
        serial_number="elegoo:abc",
        on_print_start=lambda d: events["start"].append(d),
        on_print_complete=lambda d: events["complete"].append(d),
    )
    return c, events


def test_sdcp_client_is_a_printer_client():
    c, _ = _sdcp()
    assert isinstance(c, PrinterClient)
    assert c.mainboard_id is None  # synthetic serial is not a MainboardID


def test_sdcp_client_learns_mainboard_id_and_tracks_a_print():
    c, events = _sdcp()
    c.handle_message(json.dumps(_json("sdcp_status_printing.json")))
    assert c.mainboard_id == "000000000001d354"
    assert c.state.state == "RUNNING" and len(events["start"]) == 1
    done = _json("sdcp_status_printing.json")
    done["Status"]["CurrentStatus"] = [0]
    done["Status"]["PrintInfo"]["Status"] = 9
    c.handle_message(json.dumps(done))
    assert events["complete"][0]["status"] == "completed"


def test_sdcp_client_publishes_canvas_slots():
    c, _ = _sdcp()
    c.handle_message(json.dumps(_json("sdcp_canvas_response.json")))
    assert c.has_canvas
    ams = c.state.raw_data["ams"]
    assert ams[0]["module_type"] == "canvas"
    assert ams[0]["tray"][0]["tray_type"] == "PETG"
    assert c.state.tray_now == 1


def test_sdcp_request_and_start_payload():
    c, _ = _sdcp()
    c.mainboard_id = "000000000001d354"
    req = c.build_request(128, {"Filename": "a.gcode"})
    assert req["Topic"] == "sdcp/request/000000000001d354"
    assert req["Data"]["Cmd"] == 128 and req["Data"]["MainboardID"] == "000000000001d354"
    c.has_canvas = True
    payload = c.start_print_payload("a.gcode", [1, 0], True)
    assert payload["Filename"] == "a.gcode" and payload["StartLayer"] == 0
    assert payload["slot_map"] == [{"t": 0, "canvas_id": 0, "tray_id": 1}, {"t": 1, "canvas_id": 0, "tray_id": 0}]
    c.has_canvas = False
    assert "slot_map" not in c.start_print_payload("a.gcode", None, True)


def test_sdcp_serial_field_is_only_a_fallback():
    c = ElegooSDCPClient(ip_address="192.0.2.10", serial_number="000000000001D354")
    assert c.mainboard_id is None and c._mainboard_hint == "000000000001d354"


# --------------------------------------------------------------- CC2 client
def _cc2():
    events = {"start": [], "complete": []}
    c = ElegooCC2Client(
        ip_address="192.0.2.11",
        serial_number="elegoo_cc2:abc",
        on_print_start=lambda d: events["start"].append(d),
        on_print_complete=lambda d: events["complete"].append(d),
    )
    c.sn = "CC2ABCD1234567890"
    return c, events


def test_cc2_client_is_a_printer_client():
    c, _ = _cc2()
    assert isinstance(c, PrinterClient)


def test_cc2_registration_status_and_delta_merge():
    c, events = _cc2()
    sent = []
    c._publish = lambda payload: sent.append(payload) or True
    c.handle_message(c._t("x/register_response"), json.dumps({"client_id": c._client_id, "error": "ok"}))
    assert c._registered and c.state.connected
    assert [p["method"] for p in sent] == [1002, 2005]

    c.handle_message(c._t("api_status"), json.dumps(_json("cc2_full_status.json")))
    assert c.state.state == "RUNNING" and len(events["start"]) == 1
    c.handle_message(c._t("api_status"), json.dumps(_json("cc2_delta_status.json")))
    assert c.state.layer_num == 230 and c.state.progress == 46
    assert c.state.temperatures["nozzle"] == 219.5
    assert c.state.temperatures["bed"] == 58.5  # kept from the full status

    finished = {
        "method": 6000,
        "result": {"machine_status": {"status": 1, "sub_status": 0}, "print_status": {"state": "complete"}},
    }
    c.handle_message(c._t("api_status"), json.dumps(finished))
    assert events["complete"][0]["status"] == "completed"


def test_cc2_canvas_and_start_with_slot_map():
    c, _ = _cc2()
    sent = []
    c._publish = lambda payload: sent.append(payload) or True
    c._registered = True
    c.handle_message(c._t(f"{c._client_id}/api_response"), json.dumps(_json("cc2_canvas_status.json")))
    assert c.has_canvas and c.state.raw_data["ams"][0]["module_type"] == "canvas"
    assert c.start_print("benchy.gcode", ams_mapping=[3, 1])
    cmd = sent[-1]
    assert cmd["method"] == 1020
    assert cmd["params"]["filename"] == "benchy.gcode" and cmd["params"]["storage_media"] == "local"
    assert cmd["params"]["config"]["slot_map"] == [
        {"t": 0, "canvas_id": 0, "tray_id": 3},
        {"t": 1, "canvas_id": 0, "tray_id": 1},
    ]
    assert c.state.ams_mapping == [3, 1]


def test_cc2_registration_refused_keeps_commands_off():
    c, _ = _cc2()
    c.handle_message(c._t("x/register_response"), json.dumps({"error": "too many clients"}))
    assert not c._registered and not c.start_print("a.gcode")


# --------------------------------------------------------------- capabilities
@pytest.mark.parametrize("conn", ["elegoo", "elegoo_cc2"])
def test_capabilities(conn):
    printer = type("P", (), {"connection_type": conn, "model": None})()
    caps = capabilities_for(printer)
    assert caps.can_print and caps.can_control
    assert caps.controls()["gcode"] is False and caps.controls()["home"] is False
    assert caps.controls()["temperature"] is True
    assert transport_of(printer) == conn
