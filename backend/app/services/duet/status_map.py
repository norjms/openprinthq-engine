"""Pure mapping from the RepRapFirmware object model → Bambuddy ``PrinterState``.

Both Duet transports (RRF ``rr_model`` and DSF ``/machine/status``) return the
same object model, so this mapping is shared. Unit-testable without I/O.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState

# RRF ``state.status`` → Bambuddy gcode_state vocabulary.
_DUET_STATE = {
    "idle": "IDLE",
    "off": "IDLE",
    "ready": "IDLE",
    "processing": "RUNNING",
    "printing": "RUNNING",
    "resuming": "RUNNING",
    "busy": "RUNNING",
    "changingtool": "RUNNING",
    "simulating": "RUNNING",
    "paused": "PAUSE",
    "pausing": "PAUSE",
    "halted": "FAILED",
    "error": "FAILED",
}


def map_duet_state(status: str | None) -> str:
    if not status:
        return "unknown"
    return _DUET_STATE.get(str(status).lower(), "unknown")


def _heater(heaters: list, idx: int | None) -> dict | None:
    if idx is None or not isinstance(heaters, list) or idx < 0 or idx >= len(heaters):
        return None
    h = heaters[idx]
    return h if isinstance(h, dict) else None


def apply_duet_status(state: PrinterState, model: dict) -> str:
    """Apply an RRF object model onto ``state``. Returns the mapped state."""
    if not isinstance(model, dict):
        return "unknown"

    heat = model.get("heat") or {}
    heaters = heat.get("heaters") or []
    tools = model.get("tools") or []

    # Bed heater: heat.bedHeaters[0], else heater 0.
    bed_heaters = heat.get("bedHeaters") or []
    bed_idx = bed_heaters[0] if bed_heaters else (0 if heaters else None)
    # Nozzle heater: first tool's first heater, else heater 1.
    tool_idx = None
    if tools and isinstance(tools[0], dict):
        th = tools[0].get("heaters") or []
        if th:
            tool_idx = th[0]
    if tool_idx is None:
        tool_idx = 1 if len(heaters) > 1 else None

    temps = state.temperatures
    bed = _heater(heaters, bed_idx)
    if bed:
        if bed.get("current") is not None:
            temps["bed"] = round(float(bed["current"]), 1)
        if bed.get("active") is not None:
            temps["bed_target"] = float(bed["active"])
    nozzle = _heater(heaters, tool_idx)
    if nozzle:
        if nozzle.get("current") is not None:
            temps["nozzle"] = round(float(nozzle["current"]), 1)
        if nozzle.get("active") is not None:
            target = float(nozzle["active"])
            temps["nozzle_target"] = target
            temps["nozzle_heating"] = target > 0 and temps.get("nozzle", 0) < target
    # Chamber heater if configured.
    chamber_heaters = heat.get("chamberHeaters") or []
    chamber = _heater(heaters, chamber_heaters[0] if chamber_heaters else None)
    if chamber and chamber.get("current") is not None:
        temps["chamber"] = round(float(chamber["current"]), 1)
        if chamber.get("active") is not None:
            temps["chamber_target"] = float(chamber["active"])

    status = (model.get("state") or {}).get("status")
    mapped = map_duet_state(status)
    state.state = mapped

    job = model.get("job") or {}
    jfile = job.get("file") or {}
    name = jfile.get("fileName")
    if name is not None:
        fname = name or None
        state.gcode_file = fname
        state.current_print = fname
        state.subtask_name = fname
    # Progress: filePosition / file size.
    size = jfile.get("size")
    pos = job.get("filePosition")
    if size and pos is not None:
        state.progress = round(min(100.0, float(pos) / float(size) * 100.0), 1)
    # Remaining time: timesLeft.file (seconds).
    times_left = job.get("timesLeft") or {}
    left = times_left.get("file") if isinstance(times_left, dict) else None
    if left is not None:
        state.remaining_time = max(0, round(float(left) / 60.0))
    if job.get("duration") is not None:
        state.raw_data["print_duration"] = float(job["duration"])

    return mapped
