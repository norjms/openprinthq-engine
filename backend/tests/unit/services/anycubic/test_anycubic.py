"""Anycubic LAN-mode client and ACE parsing (untested on hardware).

Fed with the reports captured on a Kobra S1 Max by anycubic_ha_local
(backend/tests/_fixtures/anycubic/); no network.
"""

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.services.anycubic.anycubic_client import AnycubicClient
from backend.app.services.anycubic.lan_handshake import LanCredentials, credentials_from, decrypt_ctrl, sign
from backend.app.services.anycubic.status_map import (
    ace_units,
    ams_box_mapping,
    apply_info,
    map_project_state,
    merge_ace,
)
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_capabilities import capabilities_for, transport_of
from backend.app.services.printer_client import PrinterClient

FIXTURES = Path(__file__).resolve().parents[3] / "_fixtures" / "anycubic"


def _json(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(
    ("state", "pause", "expected"),
    [
        ("printing", 0, "RUNNING"),
        ("printing", 1, "PAUSE"),
        ("pausing", None, "PAUSE"),
        ("resumed", 0, "RUNNING"),
        ("preheating", None, "PREPARE"),
        ("auto_leveling", None, "PREPARE"),
        ("stoped", None, "IDLE"),
        ("finished", None, "FINISH"),
        ("failed", None, "FAILED"),
    ],
)
def test_project_states(state, pause, expected):
    assert map_project_state(state, pause) == expected


def test_info_report():
    state = PrinterState()
    assert apply_info(state, _json("info_report.json")) == "RUNNING"
    assert state.temperatures["nozzle"] == 45 and state.temperatures["chamber"] == 43
    assert state.progress == 42 and (state.layer_num, state.total_layers) == (120, 900)
    assert state.remaining_time == 1800  # minutes, as the printer reports them
    assert state.gcode_file == "demo_PETG.gcode.3mf"
    assert state.firmware_version == "2.6.9.6"
    idle = PrinterState()
    assert apply_info(idle, {"state": "free", "temp": {}}) == "IDLE"


def test_ace_full_then_slim_update_merges():
    cache = merge_ace({}, _json("multicolorbox_full.json"))
    (unit,) = ace_units(cache)
    assert unit.kind == "ace" and unit.humidity == 24 and unit.temperature == 35
    s = {x.index: x for x in unit.slots}
    assert s[0].material == "PETG" and s[0].colour == "#212721" and s[0].name is None
    assert s[1].colour == "#43523B" and s[1].name == "AHPEFG-102" and s[1].remain_percent == 100
    assert not s[1].active  # loaded_slot -1
    merge_ace(cache, _json("multicolorbox_slim.json"))
    (unit,) = ace_units(cache)
    s = {x.index: x for x in unit.slots}
    assert s[1].active and s[1].remain_percent == 95
    assert s[0].material == "PETG"  # kept from the full report
    assert unit.humidity == 30  # the slim report carries it in drying_status


def test_ams_box_mapping_entries():
    units = ace_units(merge_ace({}, _json("multicolorbox_full.json")))
    assert ams_box_mapping([1, -1, 0], units) == [
        {
            "paint_index": 0,
            "ams_index": 1,
            "paint_color": [67, 82, 59, 255],
            "ams_color": [67, 82, 59, 255],
            "material_type": "PETG",
        },
        {
            "paint_index": 2,
            "ams_index": 0,
            "paint_color": [33, 39, 33, 255],
            "ams_color": [33, 39, 33, 255],
            "material_type": "PETG",
        },
    ]
    assert ams_box_mapping([7], units) == []  # no such slot


# --------------------------------------------------------------- handshake
def _encrypt(plain: dict, key: bytes, iv: bytes) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(plain).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def test_handshake_decrypt_and_credentials():
    token = "0123456789abcdefFEDCBA9876543210"
    reply_token = "rt-short"
    plain = {
        "broker": "mqtts://192.0.2.60:9883",
        "username": "user",
        "password": "pass",
        "deviceId": "dev123",
        "devicecrt": "CERT",
        "devicepk": "KEY",
    }
    blob = _encrypt(plain, token[16:32].encode(), reply_token.encode().ljust(16, b"\0"))
    assert decrypt_ctrl(blob, token, reply_token) == plain
    creds = credentials_from({"modelId": 20029, "cn": "SN1", "modelName": "Anycubic Kobra S1 Max"}, plain)
    assert (creds.broker_host, creds.broker_port, creds.device_id, creds.model_id) == (
        "192.0.2.60",
        9883,
        "dev123",
        "20029",
    )
    assert "pass" not in repr(creds) and "KEY" not in repr(creds)


def test_sign_is_the_double_md5():
    # md5(md5(token[:16]) + ts + nonce), computed independently.
    assert sign("0123456789abcdef0000", 1700000000000, "abc123") == "eed6ab6d9a86778c5e657aadf4fdabdf"


# --------------------------------------------------------------- client
def _client():
    events = {"start": [], "complete": []}
    c = AnycubicClient(
        ip_address="192.0.2.50",
        serial_number="anycubic:abc",
        on_print_start=lambda d: events["start"].append(d),
        on_print_complete=lambda d: events["complete"].append(d),
    )
    c._creds = LanCredentials(
        broker_host="192.0.2.50", broker_port=9883, username="u", device_id="dev", model_id="20029"
    )
    return c, events


def _report(msg_type):
    return f"anycubic/anycubicCloud/v1/printer/public/20029/dev/{msg_type}/report"


def test_client_is_a_printer_client():
    c, _ = _client()
    assert isinstance(c, PrinterClient)


def test_client_follows_a_print_and_the_ace():
    c, events = _client()
    c.handle_message(
        _report("info"), json.dumps({"type": "info", "action": "report", "data": _json("info_report.json")})
    )
    assert c.state.state == "RUNNING" and len(events["start"]) == 1
    c.handle_message(
        _report("print"), json.dumps({"type": "print", "action": "report", "data": _json("print_progress.json")})
    )
    assert c.state.gcode_file == "Alice_Desk_Bracket_PLA_0.2_1h12m.gcode.3mf"
    c.handle_message(
        _report("tempature"),
        json.dumps({"type": "tempature", "data": {"curr_nozzle_temp": 220, "target_nozzle_temp": 220}}),
    )
    assert c.state.temperatures["nozzle"] == 220
    c.handle_message(
        _report("multiColorBox"),
        json.dumps({"type": "multiColorBox", "action": "getInfo", "data": _json("multicolorbox_full.json")}),
    )
    assert c.has_ace and c.state.raw_data["ams"][0]["module_type"] == "ace"
    c.handle_message(
        _report("print"),
        json.dumps({"type": "print", "action": "start", "state": "finished", "data": {"taskid": "-1"}}),
    )
    assert events["complete"][0]["status"] == "completed"


def test_client_start_print_with_ace_mapping():
    c, _ = _client()
    sent = []
    c._publish = lambda kind, t, a, d: sent.append((kind, t, a, d)) or True
    c.handle_message(_report("multiColorBox"), json.dumps({"data": _json("multicolorbox_full.json")}))
    assert c.start_print("job.gcode", ams_mapping=[1])
    kind, mtype, action, data = sent[-1]
    assert (kind, mtype, action) == ("slicer", "print", "start")
    assert data["filename"] == "job.gcode" and data["filetype"] == 1 and data["taskid"] == "-1"
    assert data["ams_settings"]["use_ams"] is True
    assert data["ams_settings"]["ams_box_mapping"][0]["ams_index"] == 1
    assert c.state.ams_mapping == [1]


def test_client_start_print_without_ace_is_minimal():
    c, _ = _client()
    assert c.start_print_data("job.gcode", [1]) == {"taskid": "-1", "filename": "job.gcode", "filetype": 1}


def test_ignores_non_report_topics_and_acks():
    c, _ = _client()
    c.handle_message("anycubic/anycubicCloud/v1/printer/public/20029/dev/response", json.dumps({"msgid": "x"}))
    c.handle_message(_report("print"), json.dumps({"type": "print", "action": "update", "data": {"taskid": "-1"}}))
    assert c.state.state == "unknown"


def test_capabilities():
    printer = type("P", (), {"connection_type": "anycubic", "model": None})()
    caps = capabilities_for(printer)
    assert caps.can_print and caps.controls()["gcode"] is False and caps.controls()["home"] is False
    assert transport_of(printer) == "anycubic"
