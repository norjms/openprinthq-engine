"""Slot-by-slot remap for non-Bambu multi-material units at dispatch (#52)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.material_slots import UNIT_CFS, MaterialSlot, MaterialUnit, apply_units_to_state
from backend.app.services.print_scheduler import PrintScheduler

FIXTURES = Path(__file__).resolve().parents[1] / "_fixtures" / "material_units"
GCODE = FIXTURES / "orca_multicolour_tail.gcode"  # PLA #F0F0F0, PETG #1B1B1B, PLA (unused)


def _status():
    state = PrinterState()
    apply_units_to_state(
        state,
        [
            MaterialUnit(
                kind=UNIT_CFS,
                unit_id=0,
                slots=[
                    MaterialSlot(index=0, present=True, material="PETG", colour="#202020"),
                    MaterialSlot(index=1, present=True, material="PLA", colour="#000000"),
                    MaterialSlot(index=2, present=True, material="PLA", colour="#FFFFFF"),
                ],
            )
        ],
    )
    return state


def _item(**kw):
    base = {"id": 7, "printer_id": 3, "ams_mapping": None, "filament_overrides": None}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_remap_maps_each_gcode_filament_and_stores_it():
    item = _item()
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=_status()):
        mapping = await PrintScheduler()._external_slot_mapping(item, GCODE)
    # white PLA -> slot 2, PETG -> slot 0, unused third filament -> -1
    assert mapping == [2, 0, -1]
    assert json.loads(item.ams_mapping) == [2, 0, -1]


@pytest.mark.asyncio
async def test_overrides_change_the_wanted_colour():
    item = _item(filament_overrides=json.dumps([{"slot_id": 1, "type": "PLA", "color": "#000000"}]))
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=_status()):
        mapping = await PrintScheduler()._external_slot_mapping(item, GCODE)
    assert mapping[0] == 1


@pytest.mark.asyncio
async def test_stored_resolved_mapping_wins():
    item = _item(ams_mapping=json.dumps([1, 0]))
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=_status()):
        assert await PrintScheduler()._external_slot_mapping(item, GCODE) == [1, 0]


@pytest.mark.asyncio
async def test_no_units_or_no_match_means_no_remap(tmp_path):
    item = _item()
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=PrinterState()):
        assert await PrintScheduler()._external_slot_mapping(item, GCODE) is None

    abs_only = tmp_path / "abs.gcode"
    abs_only.write_text("; filament_type = ABS\n; filament_colour = #FF0000\n")
    with patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=_status()):
        assert await PrintScheduler()._external_slot_mapping(item, abs_only) is None
    assert item.ams_mapping is None
