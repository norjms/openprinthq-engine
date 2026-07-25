"""Pure mapping from Snapmaker HTTP /api/v1/status -> Bambuddy PrinterState.

Added by OpenPrintHQ (2026-07-25), AGPL-3.0. Kept I/O-free so it can be unit
tested against recorded /status payloads.
"""
from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState


def map_snapmaker_state(status: str | None) -> str:
    """Map Snapmaker machine status -> Bambuddy state vocabulary."""
    s = (status or "").upper()
    if s == "RUNNING":
        return "RUNNING"
    if s in ("PAUSED", "PAUSE", "PAUSING"):
        return "PAUSE"
    if s in ("IDLE", "READY", "COMPLETED", "FINISHED", "STOPPED"):
        return "IDLE"
    return "unknown"


def apply_snapmaker_status(state: PrinterState, s: dict | None) -> str:
    """Apply a Snapmaker /status response onto ``state``; return mapped state."""
    s = s if isinstance(s, dict) else {}
    temps = state.temperatures

    if s.get("nozzleTemperature") is not None:
        temps["nozzle"] = round(float(s["nozzleTemperature"]), 1)
    if s.get("nozzleTargetTemperature") is not None:
        tgt = float(s["nozzleTargetTemperature"])
        temps["nozzle_target"] = tgt
        temps["nozzle_heating"] = tgt > 0 and temps.get("nozzle", 0) < tgt
    if s.get("heatedBedTemperature") is not None:
        temps["bed"] = round(float(s["heatedBedTemperature"]), 1)
    if s.get("heatedBedTargetTemperature") is not None:
        temps["bed_target"] = float(s["heatedBedTargetTemperature"])

    mapped = map_snapmaker_state(s.get("status") or s.get("printStatus"))
    state.state = mapped

    prog = s.get("progress")
    if prog is not None:
        prog = float(prog)
        state.progress = round(prog * 100.0, 1) if prog <= 1.0 else round(prog, 1)
    if s.get("remainingTime") is not None:
        state.remaining_time = max(0, round(float(s["remainingTime"]) / 60.0))
    if s.get("elapsedTime") is not None:
        state.raw_data["print_duration"] = float(s["elapsedTime"])

    name = s.get("fileName")
    if name is not None:
        fname = name or None
        state.gcode_file = fname
        state.current_print = fname
        state.subtask_name = fname

    return mapped
