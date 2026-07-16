"""Pure mapping from OctoPrint API responses → Bambuddy ``PrinterState``.

Kept separate from the HTTP client so it can be unit-tested against recorded
``/api/printer`` + ``/api/job`` payloads without any I/O.

OctoPrint has no explicit FINISH state — a completed print returns to
``Operational`` — so completion is detected by the client from a
RUNNING→IDLE transition, not from the state text itself.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState

# OctoPrint state flags → Bambuddy gcode_state vocabulary
# (IDLE, RUNNING, PAUSE, FINISH, FAILED, unknown).


def map_octoprint_state(flags: dict) -> str:
    """Map an OctoPrint ``state.flags`` object to a Bambuddy state string."""
    if not isinstance(flags, dict):
        return "unknown"
    if flags.get("error") or flags.get("closedOrError"):
        return "FAILED"
    if flags.get("paused") or flags.get("pausing"):
        return "PAUSE"
    if flags.get("printing") or flags.get("resuming"):
        return "RUNNING"
    if flags.get("operational") or flags.get("ready"):
        return "IDLE"
    return "unknown"


def _apply_temp(temps: dict, block: dict | None, actual_key: str, target_key: str, heating_key: str | None):
    if not isinstance(block, dict):
        return
    if block.get("actual") is not None:
        temps[actual_key] = round(float(block["actual"]), 1)
    if block.get("target") is not None:
        target = float(block["target"])
        temps[target_key] = target
        if heating_key:
            temps[heating_key] = target > 0 and temps.get(actual_key, 0) < target


def apply_octoprint_status(state: PrinterState, printer_json: dict | None, job_json: dict | None) -> str:
    """Apply OctoPrint ``/api/printer`` + ``/api/job`` responses onto ``state``.

    Returns the mapped Bambuddy state string (for the client's transition
    detection). Only the transport-agnostic core fields are populated.
    """
    temps = state.temperatures
    t = (printer_json or {}).get("temperature", {}) if isinstance(printer_json, dict) else {}
    _apply_temp(temps, t.get("tool0"), "nozzle", "nozzle_target", "nozzle_heating")
    _apply_temp(temps, t.get("bed"), "bed", "bed_target", None)
    _apply_temp(temps, t.get("chamber"), "chamber", "chamber_target", "chamber_heating")

    flags = ((printer_json or {}).get("state") or {}).get("flags", {}) if isinstance(printer_json, dict) else {}
    mapped = map_octoprint_state(flags)
    state.state = mapped

    job = job_json if isinstance(job_json, dict) else {}
    progress = job.get("progress") or {}
    if progress.get("completion") is not None:
        state.progress = round(float(progress["completion"]), 1)
    if progress.get("printTimeLeft") is not None:
        state.remaining_time = max(0, round(float(progress["printTimeLeft"]) / 60.0))
    if progress.get("printTime") is not None:
        # Stash elapsed print time for the completion handler.
        state.raw_data["print_duration"] = float(progress["printTime"])

    jfile = (job.get("job") or {}).get("file") or {}
    name = jfile.get("name")
    if name is not None:
        fname = name or None
        state.gcode_file = fname
        state.current_print = fname
        state.subtask_name = fname

    return mapped
