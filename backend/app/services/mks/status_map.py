"""Pure parsing of MKS/Marlin TCP-console replies -> Bambuddy ``PrinterState``.

Standard Marlin text replies (no structured status document), so these
parsers are regex-based and tolerant of firmware variance. Unit-testable
without I/O.
"""

from __future__ import annotations

import re

from backend.app.services.bambu_mqtt import PrinterState

_TEMP_RE = re.compile(r"([TB])(\d*):\s*(-?[\d.]+)\s*/\s*(-?[\d.]+)")
_PROGRESS_RE = re.compile(r"byte\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def apply_temps(state: PrinterState, reply: str) -> None:
    """Apply an ``M105`` reply (``T:210 /210 B:60 /60``) onto ``state``."""
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


def map_m27_state(reply: str | None) -> str:
    """Map an ``M27`` (SD print status) reply to Bambuddy's state vocabulary."""
    text = (reply or "").lower()
    if "not sd printing" in text or "not printing" in text:
        return "IDLE"
    if "paused" in text or "pause" in text:
        return "PAUSE"
    if "byte" in text:
        return "RUNNING"
    return "unknown"


def apply_progress(state: PrinterState, reply: str) -> str:
    """Apply an ``M27`` reply onto ``state``. Returns the mapped state."""
    mapped = map_m27_state(reply)
    state.state = mapped
    m = _PROGRESS_RE.search(reply or "")
    if m:
        pos, size = int(m.group(1)), int(m.group(2))
        if size:
            state.progress = round(min(100.0, pos / size * 100.0), 1)
    return mapped
