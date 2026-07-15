"""Pure mapping from Moonraker status objects → Bambuddy ``PrinterState``.

Kept separate from the websocket client so it can be unit-tested against
recorded Moonraker ``notify_status_update`` payloads without any I/O.

Moonraker reports printer state as a dict of *objects* (``print_stats``,
``virtual_sdcard``, ``extruder``, ``heater_bed``, …). ``notify_status_update``
sends only the objects that changed, so updates are applied incrementally onto
a long-lived ``PrinterState``.
"""

from __future__ import annotations

from backend.app.services.bambu_mqtt import PrinterState

# Klipper print_stats.state → Bambuddy gcode_state vocabulary.
# Bambuddy uses: IDLE, RUNNING, PAUSE, FINISH, FAILED (+ PREPARE/SLICING, unused here).
KLIPPER_STATE_MAP: dict[str, str] = {
    "standby": "IDLE",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "cancelled": "FAILED",
    "error": "FAILED",
}

# Klipper raw state → the status string the Print Log expects on completion.
KLIPPER_COMPLETION_STATUS: dict[str, str] = {
    "complete": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}

# Raw Klipper states that mean "a job is actively on the bed".
ACTIVE_KLIPPER_STATES = frozenset({"printing", "paused"})


def map_print_state(klipper_state: str | None) -> str:
    """Map a Klipper ``print_stats.state`` to a Bambuddy state string."""
    if not klipper_state:
        return "unknown"
    return KLIPPER_STATE_MAP.get(klipper_state, "unknown")


def _estimate_remaining_minutes(progress: float, print_duration: float) -> int:
    """Derive remaining time (minutes) from progress + elapsed print time.

    Moonraker exposes no authoritative ETA, so extrapolate linearly from how
    long printing has taken to reach the current progress. Expect drift early
    in a print; it converges as progress climbs.
    """
    if progress <= 0.0 or progress >= 1.0 or print_duration <= 0:
        return 0
    remaining_sec = print_duration * (1.0 / progress - 1.0)
    return max(0, round(remaining_sec / 60.0))


def apply_status_objects(
    state: PrinterState,
    objects: dict,
    chamber_object: str | None = None,
) -> str | None:
    """Apply one Moonraker status dict onto ``state`` in place.

    ``chamber_object`` is the resolved Moonraker object name for the chamber
    temperature (e.g. ``"temperature_sensor chamber"`` or
    ``"temperature_fan chamber"``), discovered per-printer by the client — the
    name varies by Klipper config. ``None`` skips chamber mapping.

    Returns the raw Klipper ``print_stats.state`` seen in this update (e.g.
    ``"printing"``), or ``None`` if this update didn't touch ``print_stats`` —
    the caller uses it for transition detection (start/complete callbacks).

    Only the transport-agnostic core ``PrinterState`` fields are populated;
    AMS/drying/k-profile fields are left at their defaults.
    """
    temps = state.temperatures

    # --- temperatures ------------------------------------------------------
    extruder = objects.get("extruder")
    if isinstance(extruder, dict):
        if "temperature" in extruder:
            temps["nozzle"] = round(float(extruder["temperature"]), 1)
        if "target" in extruder:
            target = float(extruder["target"])
            temps["nozzle_target"] = target
            temps["nozzle_heating"] = target > 0 and temps.get("nozzle", 0) < target

    bed = objects.get("heater_bed")
    if isinstance(bed, dict):
        if "temperature" in bed:
            temps["bed"] = round(float(bed["temperature"]), 1)
        if "target" in bed:
            target = float(bed["target"])
            temps["bed_target"] = target

    if chamber_object:
        chamber = objects.get(chamber_object)
        if isinstance(chamber, dict):
            if "temperature" in chamber:
                temps["chamber"] = round(float(chamber["temperature"]), 1)
            # heater_generic chambers report a target; temperature_sensor ones don't.
            if "target" in chamber:
                target = float(chamber["target"])
                temps["chamber_target"] = target
                temps["chamber_heating"] = target > 0 and temps.get("chamber", 0) < target

    # --- part cooling fan (0-100%) -----------------------------------------
    fan = objects.get("fan")
    if isinstance(fan, dict) and "speed" in fan:
        state.cooling_fan_speed = round(float(fan["speed"]) * 100)

    # --- progress ----------------------------------------------------------
    # display_status.progress honours slicer M73 and is generally the best
    # single number; fall back to virtual_sdcard.progress (file position).
    progress_fraction: float | None = None
    display_status = objects.get("display_status")
    if isinstance(display_status, dict) and "progress" in display_status:
        progress_fraction = float(display_status["progress"])
    vsd = objects.get("virtual_sdcard")
    if progress_fraction is None and isinstance(vsd, dict) and "progress" in vsd:
        progress_fraction = float(vsd["progress"])
    if progress_fraction is not None:
        state.progress = round(progress_fraction * 100.0, 1)

    # --- print_stats (job state, filename, layers, duration) ---------------
    print_stats = objects.get("print_stats")
    raw_state: str | None = None
    if isinstance(print_stats, dict):
        raw_state = print_stats.get("state")
        if raw_state is not None:
            state.state = map_print_state(raw_state)

        if "filename" in print_stats:
            fname = print_stats["filename"] or None
            state.gcode_file = fname
            state.current_print = fname
            state.subtask_name = fname

        info = print_stats.get("info")
        if isinstance(info, dict):
            cur = info.get("current_layer")
            tot = info.get("total_layer")
            if isinstance(cur, int):
                state.layer_num = cur
            if isinstance(tot, int):
                state.total_layers = tot

        print_duration = print_stats.get("print_duration")
        if print_duration is not None:
            # Stash for the completion handler (duration of the finished job).
            state.raw_data["print_duration"] = float(print_duration)
            if progress_fraction is not None:
                state.remaining_time = _estimate_remaining_minutes(progress_fraction, float(print_duration))

    return raw_state
