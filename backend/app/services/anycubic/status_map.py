"""Pure mappings from Anycubic LAN-mode reports to ``PrinterState``.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware. Field names and the
lifecycle follow the captures validated on a Kobra S1 Max by anycubic_ha_local
(research/PROTOCOL-VALIDATED.md, AGPL-3.0); fixtures in
backend/tests/_fixtures/anycubic/.

Report types (topic ``.../printer/public/<model>/<device>/<type>/report``):

- ``info``: ``state`` free/busy, ``temp``, ``project`` (``state``, ``pause``,
  ``progress``, ``curr_layer``, ``total_layers``, ``remain_time`` in MINUTES,
  ``print_time`` in seconds, ``filename``). Slow (every 30 s to minutes).
- ``tempature`` (sic): the temperatures, pushed within a second of a change.
- ``print``: progress fields un-nested while a job runs; a command ack or a
  settings snapshot shares the topic, so only payloads with ``progress`` are
  progress.
- ``multiColorBox``: ACE units, ``multi_color_box[]`` with ``slots[]``
  (``index``, ``type``, ``color`` [r, g, b], ``status`` 5 loaded / 4 ready,
  ``consumables_percent``). Reports may carry a single slot; they are merged.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.material_slots import (
    SLOTS_PER_UNIT,
    UNIT_ACE,
    MaterialSlot,
    MaterialUnit,
    normalize_hex,
)

_PREPARE = {"preheating", "auto_leveling", "vibrating", "flow_calibrating", "checking", "downloading"}
_RUNNING = {"printing", "resuming", "resumed", "updated"}
_PAUSED = {"pausing", "paused"}
_STOPPED = {"stopping", "stoped", "stopped", "canceled", "cancelled"}
_FINISHED = {"finished", "complete", "completed"}
_FAILED = {"failed", "error"}


def map_project_state(project_state: str | None, pause: int | None = None, printer_state: str | None = None) -> str:
    s = str(project_state or "").lower()
    if pause in (1, 2) or s in _PAUSED:
        return "PAUSE"
    if s in _RUNNING:
        return "RUNNING"
    if s in _PREPARE:
        return "PREPARE"
    if s in _FINISHED:
        return "FINISH"
    if s in _FAILED:
        return "FAILED"
    if s in _STOPPED:
        return "IDLE"
    if str(printer_state or "").lower() == "free":
        return "IDLE"
    return "unknown" if not s and not printer_state else "IDLE"


def apply_temps(state: PrinterState, temp: dict) -> None:
    if not isinstance(temp, dict):
        return
    t = state.temperatures
    if temp.get("curr_nozzle_temp") is not None:
        t["nozzle"] = round(float(temp["curr_nozzle_temp"]), 1)
    if temp.get("target_nozzle_temp") is not None:
        tgt = float(temp["target_nozzle_temp"])
        t["nozzle_target"] = tgt
        t["nozzle_heating"] = tgt > 0 and t.get("nozzle", 0) < tgt
    if temp.get("curr_hotbed_temp") is not None:
        t["bed"] = round(float(temp["curr_hotbed_temp"]), 1)
    if temp.get("target_hotbed_temp") is not None:
        t["bed_target"] = float(temp["target_hotbed_temp"])
    if temp.get("curr_chamber_temp") is not None:
        t["chamber"] = round(float(temp["curr_chamber_temp"]), 1)


def apply_progress(state: PrinterState, p: dict) -> None:
    if not isinstance(p, dict):
        return
    if p.get("progress") is not None:
        state.progress = round(float(p["progress"]), 1)
    if isinstance(p.get("curr_layer"), int):
        state.layer_num = p["curr_layer"]
    if isinstance(p.get("total_layers"), int) and p["total_layers"] > 0:
        state.total_layers = p["total_layers"]
    if p.get("remain_time") is not None:
        state.remaining_time = max(0, int(p["remain_time"]))  # minutes
    if p.get("print_time") is not None:
        state.raw_data["print_duration"] = float(p["print_time"])
    if p.get("supplies_usage") is not None:
        state.raw_data["filament_used_mm"] = float(p["supplies_usage"])
    name = p.get("display_filename") or p.get("filename")
    if name:
        state.gcode_file = name
        state.current_print = name
        state.subtask_name = name


def apply_info(state: PrinterState, data: dict) -> str | None:
    """Apply an ``info`` report. Returns the mapped state when it says one."""
    if not isinstance(data, dict):
        return None
    apply_temps(state, data.get("temp") or {})
    if data.get("version"):
        state.firmware_version = data["version"]
    fan = data.get("fan_speed_pct")
    if fan is not None:
        state.cooling_fan_speed = int(fan)
    project = data.get("project") if isinstance(data.get("project"), dict) else None
    if project:
        apply_progress(state, project)
        mapped = map_project_state(project.get("state"), project.get("pause"), data.get("state"))
    else:
        mapped = "IDLE" if str(data.get("state") or "").lower() == "free" else None
    if mapped:
        state.state = mapped
    return mapped


# --------------------------------------------------------------------------
# ACE
# --------------------------------------------------------------------------
ACE_SLOT_LOADED = 5
ACE_SLOT_READY = 4


def merge_ace(cache: dict[int, dict], data: dict) -> dict[int, dict]:
    """Merge a ``multiColorBox`` report into ``{box_id: box}``, by slot index."""
    for box in (data or {}).get("multi_color_box") or []:
        if not isinstance(box, dict) or box.get("id") is None:
            continue
        bid = int(box["id"])
        cur = cache.setdefault(bid, {"id": bid, "slots": {}})
        for key, value in box.items():
            if key == "slots" or value is None:
                continue
            if key == "drying_status" and isinstance(value, dict) and value.get("humidity") is not None:
                cur["humidity"] = value["humidity"]
            cur[key] = value
        for slot in box.get("slots") or []:
            if isinstance(slot, dict) and slot.get("index") is not None:
                cur["slots"][int(slot["index"])] = slot
    return cache


def ace_units(cache: dict[int, dict]) -> list[MaterialUnit]:
    units = []
    for bid in sorted(cache):
        box = cache[bid]
        unit = MaterialUnit(
            kind=UNIT_ACE,
            unit_id=bid,
            humidity=box.get("humidity"),
            temperature=box.get("temp"),
        )
        loaded = box.get("loaded_slot")
        for idx in range(SLOTS_PER_UNIT):
            raw = box["slots"].get(idx)
            if raw is None:
                continue
            material = str(raw.get("type") or "").strip().upper() or None
            present = material is not None
            unit.slots.append(
                MaterialSlot(
                    index=idx,
                    present=present,
                    material=material,
                    colour=normalize_hex(raw.get("color")) if present else None,
                    name=(raw.get("sku") or None) if present else None,
                    remain_percent=raw.get("consumables_percent") if present else None,
                    active=present and (raw.get("status") == ACE_SLOT_LOADED and loaded == idx),
                )
            )
        units.append(unit)
    return units


def _rgba(colour: str | None) -> list[int]:
    hexv = normalize_hex(colour) or "#FFFFFF"
    return [int(hexv[1:3], 16), int(hexv[3:5], 16), int(hexv[5:7], 16), 255]


def ams_box_mapping(mapping: list[int], units: list[MaterialUnit]) -> list[dict]:
    """``ams_settings.ams_box_mapping`` for a remap (position = G-code tool).

    One entry per remapped tool: ``paint_index`` the G-code filament,
    ``ams_index`` the global ACE slot, the colours as [r, g, b, a] and the
    material, the shape Anycubic's slicer sends (Rinkhals MQTT notes). The
    remap only pairs a filament with a slot of its own material, so the slot's
    material and colour stand for both sides.
    """
    slots = {u.global_slot(s.index): s for u in units for s in u.slots}
    out = []
    for tool, slot in enumerate(mapping or []):
        if slot is None or slot < 0 or slot not in slots:
            continue
        target = slots[slot]
        colour = _rgba(target.colour)
        out.append(
            {
                "paint_index": tool,
                "ams_index": slot,
                "paint_color": colour,
                "ams_color": colour,
                "material_type": (target.material or "").upper(),
            }
        )
    return out
