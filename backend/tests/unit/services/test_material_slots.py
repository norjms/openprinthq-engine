"""Vendor-neutral multi-material slot model and the #52 slot remap."""

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.material_slots import (
    UNIT_CFS,
    MaterialSlot,
    MaterialUnit,
    apply_units_to_state,
    colour_distance,
    normalize_hex,
    remap_slots,
    units_from_state,
    units_to_ams,
)
from backend.app.services.printer_manager import printer_state_to_dict


def _unit(slots, unit_id=0, kind=UNIT_CFS):
    return MaterialUnit(kind=kind, unit_id=unit_id, slots=slots)


def test_normalize_hex_handles_vendor_spellings():
    assert normalize_hex("#ff00aa") == "#FF00AA"
    assert normalize_hex("ff00aa") == "#FF00AA"
    assert normalize_hex("0C12E1F") == "#C12E1F"  # Creality pad digit
    assert normalize_hex("FF00AAFF") == "#FF00AA"  # Bambu RRGGBBAA
    assert normalize_hex([255, 0, 170]) == "#FF00AA"  # Anycubic RGB list
    assert normalize_hex([255, 0, 170, 255]) == "#FF00AA"
    assert normalize_hex("#0ffa800,#0ff97e1") is None  # multi-colour spool
    assert normalize_hex("") is None
    assert normalize_hex(None) is None
    assert normalize_hex("nonsense") is None


def test_colour_distance_orders_closer_colours_first():
    assert colour_distance("#FF0000", "#FF0000") == 0
    assert colour_distance("#FF0000", "#EE1111") < colour_distance("#FF0000", "#0000FF")
    assert colour_distance(None, "#FFFFFF") > colour_distance("#000000", "#FFFFFF")


def test_units_project_onto_bambu_tray_shape():
    unit = _unit(
        [
            MaterialSlot(index=0, present=True, material="PLA", colour="#FFFFFF", remain_percent=80),
            MaterialSlot(index=1, present=False),
        ]
    )
    unit.humidity = 41.6
    unit.temperature = 27
    (ams,) = units_to_ams([unit])
    assert ams["module_type"] == "cfs"
    assert ams["humidity_raw"] == 42
    assert len(ams["tray"]) == 4
    assert ams["tray"][0]["tray_type"] == "PLA"
    assert ams["tray"][0]["tray_color"] == "FFFFFFFF"
    assert ams["tray"][0]["remain"] == 80
    # Absent and unreported slots carry only their id: the card's empty signal.
    assert ams["tray"][1] == {"id": 1}
    assert ams["tray"][3] == {"id": 3}


def test_state_round_trip_and_status_payload():
    state = PrinterState()
    unit = _unit(
        [
            MaterialSlot(index=0, present=True, material="PETG", colour="#1A1A1A"),
            MaterialSlot(index=2, present=True, material="PLA", colour="#FF0000", active=True),
        ],
        unit_id=1,
    )
    apply_units_to_state(state, [unit])
    assert state.tray_now == 6  # unit 1, slot 2
    rebuilt = units_from_state(state)
    assert rebuilt[0].unit_id == 1 and rebuilt[0].slots[1].material == "PLA"

    payload = printer_state_to_dict(state)
    assert payload["ams"][0]["module_type"] == "cfs"
    assert payload["ams"][0]["tray"][0]["tray_type"] == "PETG"
    assert payload["ams"][0]["tray"][1]["state"] == 9  # empty slot


def test_disconnected_units_clear_state():
    state = PrinterState()
    apply_units_to_state(state, [_unit([MaterialSlot(index=0, present=True, material="PLA")])])
    assert state.raw_data["ams"]
    gone = _unit([])
    gone.connected = False
    apply_units_to_state(state, [gone])
    assert "ams" not in state.raw_data
    assert state.tray_now == 255


def test_remap_picks_same_material_and_closest_colour():
    units = [
        _unit(
            [
                MaterialSlot(index=0, present=True, material="PLA", colour="#FFFFFF"),
                MaterialSlot(index=1, present=True, material="PETG", colour="#202020"),
                MaterialSlot(index=2, present=True, material="PLA", colour="#E01010"),
                MaterialSlot(index=3, present=True, material="PLA", colour="#101010"),
            ]
        )
    ]
    required = [
        {"tool": 0, "type": "PLA", "color": "#FF0000", "used": True},
        {"tool": 1, "type": "PLA", "color": "#000000", "used": True},
        {"tool": 2, "type": "PETG", "color": "#FFFFFF", "used": True},
    ]
    # Red -> red PLA, black -> black PLA, PETG -> the only PETG whatever its colour.
    assert remap_slots(required, units) == [2, 3, 1]


def test_remap_never_crosses_material_and_marks_missing():
    units = [_unit([MaterialSlot(index=0, present=True, material="PLA", colour="#FF0000")])]
    required = [{"tool": 0, "type": "ABS", "color": "#FF0000", "used": True}]
    assert remap_slots(required, units) == [-1]


def test_remap_skips_unused_filaments_and_prefers_free_slots():
    units = [
        _unit(
            [
                MaterialSlot(index=0, present=True, material="PLA", colour="#FFFFFF"),
                MaterialSlot(index=1, present=True, material="PLA", colour="#F0F0F0"),
            ]
        )
    ]
    required = [
        {"tool": 0, "type": "PLA", "color": "#FFFFFF", "used": True},
        {"tool": 1, "type": "PLA", "color": "#FFFFFF", "used": True},
        {"tool": 2, "type": "PLA", "color": "#FFFFFF", "used": False},
    ]
    assert remap_slots(required, units) == [0, 1, -1]


def test_remap_reuses_a_slot_only_when_nothing_else_fits():
    units = [_unit([MaterialSlot(index=0, present=True, material="PLA", colour="#FFFFFF")])]
    required = [
        {"tool": 0, "type": "PLA", "color": "#FFFFFF", "used": True},
        {"tool": 1, "type": "PLA", "color": "#FFFFFF", "used": True},
    ]
    assert remap_slots(required, units) == [0, 0]


def test_remap_uses_scheduler_equivalence_groups():
    units = [_unit([MaterialSlot(index=0, present=True, material="PA12-CF", colour="#000000")], unit_id=2)]
    required = [{"tool": 0, "type": "PAHT-CF", "color": "#000000", "used": True}]
    assert remap_slots(required, units) == [8]
