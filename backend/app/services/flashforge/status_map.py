"""Pure parsing of FlashForge legacy TCP replies -> Bambuddy ``PrinterState``.

The protocol has no structured status document — each ``~M1xx`` command
returns a short text block. These parsers are tolerant of firmware variance
(field order, spacing) since the exact wording differs across FlashForge
firmware revisions. Unit-testable without I/O.
"""

from __future__ import annotations

import re

from backend.app.services.bambu_mqtt import PrinterState

# ``~M119`` "MachineStatus:" -> Bambuddy gcode_state vocabulary.
_FF_STATE = {
    "ready": "IDLE",
    "busy": "IDLE",
    "building_from_sd": "RUNNING",
    "building": "RUNNING",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "pause": "PAUSE",
    "building_completed": "IDLE",
    "build_completed": "IDLE",
    "error": "FAILED",
}

_TEMP_RE = re.compile(r"([TB])(\d*):\s*([\d.]+)\s*/\s*([\d.]+)")
_PROGRESS_RE = re.compile(r"byte\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_MACHINE_STATUS_RE = re.compile(r"MachineStatus:\s*(\S+)", re.IGNORECASE)
_CURRENT_FILE_RE = re.compile(r"CurrentFile:\s*(\S+)", re.IGNORECASE)


def map_ff_state(status: str | None) -> str:
    if not status:
        return "unknown"
    return _FF_STATE.get(str(status).strip().lower(), "unknown")


def apply_temps(state: PrinterState, reply: str) -> None:
    """Apply a ``~M105`` reply (``T0:210 /210 B:60/60``) onto ``state``."""
    temps = state.temperatures
    for prefix, _idx, current, target in _TEMP_RE.findall(reply or ""):
        cur = round(float(current), 1)
        tgt = float(target)
        if prefix == "T":
            temps["nozzle"] = cur
            temps["nozzle_target"] = tgt
            temps["nozzle_heating"] = tgt > 0 and cur < tgt
        elif prefix == "B":
            temps["bed"] = cur
            temps["bed_target"] = tgt


def apply_progress(state: PrinterState, reply: str) -> None:
    """Apply a ``~M27`` reply (``SD printing byte 1234/56789``) onto ``state``."""
    m = _PROGRESS_RE.search(reply or "")
    if m:
        pos, size = int(m.group(1)), int(m.group(2))
        if size:
            state.progress = round(min(100.0, pos / size * 100.0), 1)


def apply_machine_status(state: PrinterState, reply: str) -> str:
    """Apply a ``~M119`` reply onto ``state``. Returns the mapped state."""
    m = _MACHINE_STATUS_RE.search(reply or "")
    mapped = map_ff_state(m.group(1) if m else None)
    state.state = mapped

    f = _CURRENT_FILE_RE.search(reply or "")
    if f:
        name = f.group(1)
        state.gcode_file = name
        state.current_print = name
        state.subtask_name = name
    return mapped
