"""Creality CFS and QIDI Box parsing on Moonraker printers (untested on hardware)."""

import json
from pathlib import Path

from backend.app.services.klipper.material_units import (
    MaterialObjectTracker,
    QidiTables,
    cfs_remap_gcode,
    cfs_slot_name,
    parse_cfs_box,
    parse_qidi,
    qidi_remap_gcode,
)
from backend.app.services.klipper.moonraker_client import MoonrakerClient

FIXTURES = Path(__file__).resolve().parents[3] / "_fixtures" / "material_units"


def _json(name):
    return json.loads((FIXTURES / name).read_text())


def test_cfs_stock_schema():
    units = parse_cfs_box(_json("cfs_k2plus_box_stock.json"))
    assert len(units) == 1  # T2 is not connected
    unit = units[0]
    assert unit.kind == "cfs" and unit.unit_id == 0
    assert unit.humidity == 48 and unit.temperature == 27
    assert unit.serial == "10000882925" and unit.firmware == "1.1.3"
    s = unit.slots
    assert (s[0].present, s[0].material, s[0].colour) == (True, "PLA", "#000000")
    assert (s[1].present, s[1].material, s[1].colour) == (True, "PETG", "#0A2989")  # no tag: "unknown"
    assert (s[2].material, s[2].colour) == ("PLA", "#FFFFFF")
    assert s[3].present is False and s[3].material is None


def test_cfs_unknown_code_uses_same_material_group_then_generic_suffix():
    box = {
        "same_material": [["104099", "0123456", ["T1A"], "PETG-CF"]],
        "T1": {
            "state": "connect",
            "vender": ["unknown", "unknown", "unknown", "unknown"],
            "material_type": ["104099", "100005", "000003", "999999"],
            "color_value": ["0123456", "0FFFFFF", "0000000", "0FF0000"],
        },
    }
    slots = parse_cfs_box(box)[0].slots
    assert [x.material for x in slots] == ["PETG-CF", "TPU", "PETG", None]
    assert all(x.present for x in slots)


def test_cfs_flat_schema_skips_external_spool():
    units = parse_cfs_box(_json("cfs_box_flat.json"))
    assert len(units) == 1
    slots = {x.index: x for x in units[0].slots}
    assert set(slots) == {0, 1, 2, 3}
    assert slots[1].active and slots[1].material == "PLA" and slots[1].brand == "Creality"
    assert slots[0].brand is None and slots[0].material == "PETG"
    assert slots[2].present is False
    assert units[0].temperature == 26.5 and units[0].humidity == 41.0


def test_cfs_tracker_merges_delta_frames():
    t = MaterialObjectTracker()
    t.detect(["print_stats", "box", "extruder"])
    assert t.kind == "cfs"
    assert t.subscribe_objects() == {"box": None}
    assert t.update({"box": _json("cfs_k2plus_box_stock.json")})
    # A later frame carries only the field that changed.
    assert t.update({"box": {"T1": {"color_value": ["0FF0000", "00A2989", "0FFFFFF", "-1"]}}})
    assert t.units()[0].slots[0].colour == "#FF0000"
    assert t.units()[0].slots[1].material == "PETG"
    assert not t.update({"extruder": {"temperature": 200}})


def test_cfs_slot_names_and_remap_gcode():
    assert cfs_slot_name(0) == "T1A"
    assert cfs_slot_name(5) == "T2B"
    assert cfs_slot_name(15) == "T4D"
    assert cfs_remap_gcode([2, -1, 5]) == "BOX_MODIFY_TN T1A=T1C\nBOX_MODIFY_TN T1B=T1B\nBOX_MODIFY_TN T1C=T2B"


def _qidi_tables():
    return QidiTables((FIXTURES / "qidi_officiall_filas_list.cfg").read_text())


def test_qidi_tables_parse_stock_file():
    tables = _qidi_tables()
    assert tables.filaments[11]["type"] == "ABS"
    assert tables.colours[18] == "#FF362D"
    assert tables.vendors[2] == "eSUN"


def test_qidi_box_parse():
    controller = {
        "box": {
            "box0_filament_info_3_filament_id": "QFA90",
            "box0_filament_info_3_filament_type": "PETG",
            "box0_filament_info_3_vendor_str": "QIDI",
            "box0_filament_info_3_min_temp": 230,
        }
    }
    units = parse_qidi(
        _json("qidi_save_variables.json"),
        controller,
        {0: {"temperature": 31.2, "humidity": 22}},
        _qidi_tables(),
    )
    assert len(units) == 1
    unit = units[0]
    assert unit.kind == "qidi_box" and unit.humidity == 22 and unit.temperature == 31.2
    s = unit.slots
    assert (s[0].material, s[0].colour, s[0].brand, s[0].name) == ("PLA", "#FF362D", "QIDI", "PLA Rapido")
    assert (s[1].material, s[1].colour, s[1].brand, s[1].active) == ("ABS", "#060606", "eSUN", True)
    assert s[2].present is False
    # Product id + colour list written by newer box firmware.
    assert (s[3].material, s[3].colour, s[3].brand, s[3].nozzle_temp_min) == ("PETG", "#228332", "QIDI", 230)


def test_qidi_without_box_count_has_no_units():
    assert parse_qidi({"slot0": 1}) == []
    assert parse_qidi({"box_count": 0}) == []


def test_qidi_tracker_detects_from_box_steppers():
    t = MaterialObjectTracker()
    t.detect(["box_stepper slot0", "box_stepper slot1", "save_variables", "aht20_f heater_box1"])
    assert t.kind == "qidi_box"
    objs = t.subscribe_objects()
    assert "save_variables" in objs and "aht20_f heater_box1" in objs
    assert "multi_color_controller" not in objs
    t.qidi_tables = _qidi_tables()
    assert t.update({"save_variables": {"variables": _json("qidi_save_variables.json")}})
    assert t.update({"aht20_f heater_box1": {"temperature": 30.0, "humidity": 18.0}})
    unit = t.units()[0]
    assert unit.humidity == 18.0 and unit.slots[0].material == "PLA"


def test_qidi_remap_gcode_quotes_the_value():
    assert qidi_remap_gcode([3, -1]) == (
        "SAVE_VARIABLE VARIABLE=value_t0 VALUE='\"slot3\"'\nSAVE_VARIABLE VARIABLE=value_t1 VALUE='\"slot1\"'"
    )


def test_no_unit_detected_is_a_noop():
    t = MaterialObjectTracker()
    t.detect(["print_stats", "extruder"])
    assert t.kind is None and t.subscribe_objects() == {} and t.units() == []
    assert t.remap_gcode([1, 2]) == ""


def _client():
    return MoonrakerClient(ip_address="192.0.2.5", model="voron_2.4_350", serial_number="klipper:test")


def test_moonraker_client_publishes_cfs_slots_into_state():
    c = _client()
    c._materials.detect(["box"])
    assert "box" in c._subscribe_objects()
    c._apply_and_signal({"box": _json("cfs_k2plus_box_stock.json")})
    ams = c.state.raw_data["ams"]
    assert ams[0]["module_type"] == "cfs"
    assert ams[0]["tray"][1]["tray_type"] == "PETG"
    assert c.material_unit_kind == "cfs"


def test_moonraker_start_print_sends_remap_and_start_in_one_script():
    c = _client()
    c._materials.detect(["box"])
    sent = []
    c._send_rpc = lambda method, params=None: sent.append((method, params)) or True
    assert c.start_print("job.gcode", ams_mapping=[2, 0])
    assert sent == [
        (
            "printer.gcode.script",
            {"script": 'BOX_MODIFY_TN T1A=T1C\nBOX_MODIFY_TN T1B=T1A\nSDCARD_PRINT_FILE FILENAME="job.gcode"'},
        )
    ]
    assert c.state.ams_mapping == [2, 0]


def test_moonraker_start_print_without_unit_is_unchanged():
    c = _client()
    sent = []
    c._send_rpc = lambda method, params=None: sent.append((method, params)) or True
    assert c.start_print("job.gcode", ams_mapping=[2, 0])
    assert sent == [("printer.print.start", {"filename": "job.gcode"})]
