"""Pure mappings from Elegoo status payloads to ``PrinterState``.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware: built from the published
protocol documents listed in docs/elegoo-printers.md and checked against the
payloads in backend/tests/_fixtures/elegoo/.

Two protocols:

SDCP v3 (Centauri Carbon, WebSocket on :3030)
    ``Status`` carries ``CurrentStatus`` (a list of machine states), the
    temperatures and ``PrintInfo`` (sub-state, layers, ticks, file name).
    CANVAS units are read with Cmd 324.

Centauri Carbon 2 (MQTT broker on the printer, :1883)
    Method 6000 pushes a full status once and deltas after it, which the client
    merges; ``print_status.state`` is Klipper's own print state. CANVAS units
    are read with method 2005.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.material_slots import (
    SLOTS_PER_UNIT,
    UNIT_CANVAS,
    MaterialSlot,
    MaterialUnit,
    normalize_hex,
)

# --------------------------------------------------------------------------
# SDCP v3 (Centauri Carbon)
# --------------------------------------------------------------------------
# Machine status (CurrentStatus) - SDCP v3 / elegoo-homeassistant enums.
SDCP_MACHINE_IDLE = 0
SDCP_MACHINE_PRINTING = 1
SDCP_MACHINE_FILE_TRANSFER = 2

# PrintInfo.Status sub-states.
_SDCP_PAUSED = {5, 6}  # pausing, paused
_SDCP_STOPPED = {7, 8}  # stopping, stopped
_SDCP_COMPLETE = 9
_SDCP_ERROR = 114
_SDCP_PREPARE = {1, 10, 15, 16, 20, 111, 117, 118, 119, 120, 121, 122, 123}

# TotalTicks above this are milliseconds (the SDCP v3 document says ms; some
# FDM firmware reports seconds). 400000 ms is under 7 minutes, 400000 s is
# over 111 hours, so the two readings never overlap for a real print.
_TICKS_MS_THRESHOLD = 400_000


def _ticks_to_seconds(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > _TICKS_MS_THRESHOLD else v


def map_sdcp_state(current_status, print_status) -> str:
    """Map SDCP machine + print sub-state to the engine state vocabulary."""
    statuses = current_status if isinstance(current_status, list) else [current_status]
    statuses = [s for s in statuses if isinstance(s, int)]
    # A file transfer can sit in front of the real state.
    main = next((s for s in statuses if s != SDCP_MACHINE_FILE_TRANSFER), statuses[0] if statuses else None)
    if main == SDCP_MACHINE_PRINTING or print_status in _SDCP_PAUSED or print_status == 3:
        if print_status in _SDCP_PAUSED:
            return "PAUSE"
        if print_status == _SDCP_COMPLETE:
            return "FINISH"
        if print_status in _SDCP_STOPPED:
            return "IDLE"
        if print_status == _SDCP_ERROR:
            return "FAILED"
        if print_status in _SDCP_PREPARE:
            return "PREPARE"
        return "RUNNING"
    if main == SDCP_MACHINE_IDLE:
        return "FINISH" if print_status == _SDCP_COMPLETE else "IDLE"
    if main is None:
        return "unknown"
    return "IDLE"  # homing, levelling, self-test and the like: not a print


def apply_sdcp_status(state: PrinterState, status: dict) -> str:
    """Apply an SDCP ``Status`` object; returns the mapped state."""
    s = status if isinstance(status, dict) else {}
    temps = state.temperatures
    if s.get("TempOfNozzle") is not None:
        temps["nozzle"] = round(float(s["TempOfNozzle"]), 1)
    if s.get("TempTargetNozzle") is not None:
        tgt = float(s["TempTargetNozzle"])
        temps["nozzle_target"] = tgt
        temps["nozzle_heating"] = tgt > 0 and temps.get("nozzle", 0) < tgt
    if s.get("TempOfHotbed") is not None:
        temps["bed"] = round(float(s["TempOfHotbed"]), 1)
    if s.get("TempTargetHotbed") is not None:
        temps["bed_target"] = float(s["TempTargetHotbed"])
    if s.get("TempOfBox") is not None:
        temps["chamber"] = round(float(s["TempOfBox"]), 1)

    info = s.get("PrintInfo") if isinstance(s.get("PrintInfo"), dict) else {}
    print_status = info.get("Status")
    mapped = map_sdcp_state(s.get("CurrentStatus"), print_status)
    state.state = mapped

    if isinstance(info.get("CurrentLayer"), int):
        state.layer_num = info["CurrentLayer"]
    if isinstance(info.get("TotalLayer"), int):
        state.total_layers = info["TotalLayer"]
    if info.get("Filename") is not None:
        fname = info.get("Filename") or None
        state.gcode_file = fname
        state.current_print = fname
        state.subtask_name = fname
    cur = _ticks_to_seconds(info.get("CurrentTicks"))
    total = _ticks_to_seconds(info.get("TotalTicks"))
    if info.get("Progress") is not None:
        try:
            state.progress = round(float(info["Progress"]), 1)
        except (TypeError, ValueError):
            pass
    elif cur is not None and total:
        state.progress = round(min(100.0, cur / total * 100.0), 1)
    if cur is not None:
        state.raw_data["print_duration"] = cur
    if cur is not None and total is not None:
        state.remaining_time = max(0, round((total - cur) / 60.0))
    if info.get("ErrorNumber"):
        state.raw_data["sdcp_error_number"] = info.get("ErrorNumber")
    return mapped


# --------------------------------------------------------------------------
# Centauri Carbon 2
# --------------------------------------------------------------------------
_CC2_PRINT_STATE = {
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "cancelled": "IDLE",
    "error": "FAILED",
    "standby": "IDLE",
}
CC2_MACHINE_IDLE = 1
CC2_MACHINE_PRINTING = 2
_CC2_PAUSED_SUB = {2501, 2502, 2505}
_CC2_PREPARE_SUB = {1045, 1096, 1405, 1906, 2801, 2901}


def map_cc2_state(machine: dict, print_status: dict) -> str:
    raw = str((print_status or {}).get("state") or "").lower()
    status = (machine or {}).get("status")
    sub = (machine or {}).get("sub_status")
    if status == CC2_MACHINE_PRINTING:
        if sub in _CC2_PAUSED_SUB or raw == "paused":
            return "PAUSE"
        if sub == 2077 or raw == "complete":
            return "FINISH"
        if sub in (2503, 2504) or raw == "cancelled":
            return "IDLE"
        if raw == "error":
            return "FAILED"
        if sub in _CC2_PREPARE_SUB:
            return "PREPARE"
        return "RUNNING"
    if raw in _CC2_PRINT_STATE and raw not in ("printing", "paused"):
        return _CC2_PRINT_STATE[raw]
    if status is None and not raw:
        return "unknown"
    return "IDLE"


def apply_cc2_status(state: PrinterState, result: dict) -> str:
    """Apply a merged CC2 status (method 1002 / 6000 ``result``)."""
    r = result if isinstance(result, dict) else {}
    temps = state.temperatures
    extruder = r.get("extruder") if isinstance(r.get("extruder"), dict) else {}
    bed = r.get("heater_bed") if isinstance(r.get("heater_bed"), dict) else {}
    chamber = r.get("ztemperature_sensor") if isinstance(r.get("ztemperature_sensor"), dict) else {}
    if extruder.get("temperature") is not None:
        temps["nozzle"] = round(float(extruder["temperature"]), 1)
    if extruder.get("target") is not None:
        tgt = float(extruder["target"])
        temps["nozzle_target"] = tgt
        temps["nozzle_heating"] = tgt > 0 and temps.get("nozzle", 0) < tgt
    if bed.get("temperature") is not None:
        temps["bed"] = round(float(bed["temperature"]), 1)
    if bed.get("target") is not None:
        temps["bed_target"] = float(bed["target"])
    if chamber.get("temperature") is not None:
        temps["chamber"] = round(float(chamber["temperature"]), 1)

    machine = r.get("machine_status") if isinstance(r.get("machine_status"), dict) else {}
    ps = r.get("print_status") if isinstance(r.get("print_status"), dict) else {}
    mapped = map_cc2_state(machine, ps)
    state.state = mapped

    if isinstance(ps.get("current_layer"), int):
        state.layer_num = ps["current_layer"]
    if isinstance(ps.get("total_layer"), int) and ps["total_layer"] > 0:
        state.total_layers = ps["total_layer"]
    # machine_status.progress is the one a delta update carries.
    progress = machine.get("progress", ps.get("progress"))
    if progress is not None:
        try:
            state.progress = round(float(progress), 1)
        except (TypeError, ValueError):
            pass
    if ps.get("remaining_time_sec") is not None:
        state.remaining_time = max(0, round(float(ps["remaining_time_sec"]) / 60.0))
    if ps.get("print_duration") is not None:
        state.raw_data["print_duration"] = float(ps["print_duration"])
    if ps.get("filament_used") is not None:
        state.raw_data["filament_used_mm"] = float(ps["filament_used"])
    if "filename" in ps:
        fname = ps.get("filename") or None
        state.gcode_file = fname
        state.current_print = fname
        state.subtask_name = fname
    fan = (r.get("fans") or {}).get("fan") if isinstance(r.get("fans"), dict) else None
    if isinstance(fan, dict) and fan.get("speed") is not None:
        state.cooling_fan_speed = round(float(fan["speed"]) / 255 * 100)
    return mapped


def merge_delta(dst: dict, src: dict) -> dict:
    """Deep-merge a CC2 delta update into the cached full status."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge_delta(dst[key], value)
        else:
            dst[key] = value
    return dst


# --------------------------------------------------------------------------
# CANVAS (both protocols use the same canvas_info shape)
# --------------------------------------------------------------------------
_PLACEHOLDERS = {"", "?", "-", "None", "none", "null"}


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    if text in _PLACEHOLDERS or text.startswith("—") or set(text) <= {"-", " "}:
        return None
    return text


def parse_canvas(canvas_info: dict) -> list[MaterialUnit]:
    """``canvas_info`` (``canvas_list`` / ``tray_list``) -> units.

    Tray ``status``: 0 empty, 1 filament present, 2 feeding the toolhead.
    Centauri Carbon 1 reports dash and ``?`` placeholders for unknown fields.
    """
    if not isinstance(canvas_info, dict):
        return []
    units: list[MaterialUnit] = []
    active = (canvas_info.get("active_canvas_id"), canvas_info.get("active_tray_id"))
    for canvas in canvas_info.get("canvas_list") or []:
        if not isinstance(canvas, dict):
            continue
        try:
            unit_id = int(canvas.get("canvas_id", 0))
        except (TypeError, ValueError):
            continue
        unit = MaterialUnit(kind=UNIT_CANVAS, unit_id=unit_id, connected=bool(canvas.get("connected", 1)))
        for tray in canvas.get("tray_list") or []:
            if not isinstance(tray, dict):
                continue
            try:
                idx = int(tray.get("tray_id", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < SLOTS_PER_UNIT:
                continue
            status = tray.get("status")
            material = _clean(tray.get("filament_type"))
            present = status in (1, 2) or (status is None and material is not None)
            unit.slots.append(
                MaterialSlot(
                    index=idx,
                    present=present,
                    material=material.upper() if present and material else None,
                    colour=normalize_hex(tray.get("filament_color")) if present else None,
                    name=_clean(tray.get("filament_name")) if present else None,
                    brand=_clean(tray.get("brand")) if present else None,
                    active=present and (status == 2 or active == (unit_id, idx)),
                    nozzle_temp_min=tray.get("min_nozzle_temp") or None if present else None,
                    nozzle_temp_max=tray.get("max_nozzle_temp") or None if present else None,
                )
            )
        units.append(unit)
    return units


def canvas_slot_map(mapping: list[int]) -> list[dict]:
    """Remap (position = G-code tool) -> ``slot_map`` entries for start print.

    Every entry carries ``t``, ``canvas_id`` and ``tray_id`` together (a
    partial entry is acknowledged and then printed from tray 0). Tools with no
    slot (-1) are left out so the printer keeps its own choice for them.
    """
    out = []
    for tool, slot in enumerate(mapping or []):
        if slot is None or slot < 0:
            continue
        out.append({"t": tool, "canvas_id": slot // SLOTS_PER_UNIT, "tray_id": slot % SLOTS_PER_UNIT})
    return out
