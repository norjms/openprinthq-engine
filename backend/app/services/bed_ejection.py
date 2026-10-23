"""Server-side bed ejection (OpenPrintHQ farm feature).

After a successful print on a printer with ``auto_eject`` on and an ejection
profile set, this runs the profile and, only if every step worked, clears the
plate-clear gate so the queue can start the next job unattended:

1. eligibility: the print completed (never after a failure or a cancel, which
   may have left a mess the macro would plough into), the printer's installed
   plate is one the profile allows, and the consecutive-eject limit is not hit;
2. wait for the bed to cool to the profile's temperature (parts release cold);
3. send the profile's G-code through the printer client;
4. wait for the printer to report idle again;
5. optionally confirm with the camera plate detector that the bed is empty;
6. clear the gate and count the eject.

Any failure, timeout or refusal leaves the gate raised, so the printer waits for
a person exactly as it would without auto-eject. While an eject is running the
printer is in ``EJECTING`` and the scheduler treats it as busy whatever the
``require_plate_clear`` setting says, so a job can't start under a moving
toolhead. A person clearing the plate by hand resets the consecutive count.

Tests replace the printer client and the clock; nothing here is ever exercised
against real hardware by the suite.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.app.core.database import async_session
from backend.app.models.ejection_profile import EjectionProfile
from backend.app.models.printer import Printer
from backend.app.services.farm_match import normalize_plate
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

IDLE_STATES = ("IDLE", "FINISH", "FAILED")
POLL_S = 5.0

# printer_id -> True while an eject runs. Set synchronously by start() so the
# scheduler sees it before the completion callback returns.
EJECTING: set[int] = set()


@dataclass
class EjectResult:
    printer_id: int
    ok: bool
    reason: str
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None


# printer_id -> last result, for GET /printers/{id}/eject and the UI.
LAST_RESULT: dict[int, EjectResult] = {}


def is_ejecting(printer_id: int) -> bool:
    return printer_id in EJECTING


def eligibility(printer, profile, status: str) -> str | None:
    """Why this print must not be ejected automatically, or None if it may."""
    if not getattr(printer, "auto_eject", False):
        return "auto-eject is off"
    if profile is None:
        return "no ejection profile set"
    if status != "completed":
        return f"the print ended '{status}', only completed prints are ejected"
    allowed = [p for p in (profile.allowed_plates or []) if p]
    if allowed:
        installed = normalize_plate(getattr(printer, "installed_plate", None))
        if not installed:
            return "the profile is limited to certain plates and the printer has no installed plate set"
        if installed not in {normalize_plate(p) for p in allowed}:
            return f"the installed plate ({printer.installed_plate}) is not one the profile allows"
    cap = profile.max_consecutive or 0
    if cap and (getattr(printer, "consecutive_ejects", 0) or 0) >= cap:
        return f"{cap} ejects in a row; a person has to check the bed and clear the plate"
    return None


def _bed_temp(printer_id: int) -> float | None:
    st = printer_manager.get_status(printer_id)
    if not st:
        return None
    try:
        v = (st.temperatures or {}).get("bed")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _state(printer_id: int) -> str | None:
    st = printer_manager.get_status(printer_id)
    return getattr(st, "state", None) if st else None


async def _wait_until(pred, timeout_s: float, sleep=asyncio.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + max(0.0, timeout_s)
    while True:
        if pred():
            return True
        if clock() >= deadline:
            return False
        await sleep(POLL_S)


async def _verify_empty(printer) -> tuple[bool, str]:
    from backend.app.services.plate_detection import check_plate_empty

    roi = None
    if all(
        getattr(printer, k, None) is not None
        for k in ("plate_detection_roi_x", "plate_detection_roi_y", "plate_detection_roi_w", "plate_detection_roi_h")
    ):
        roi = (
            printer.plate_detection_roi_x,
            printer.plate_detection_roi_y,
            printer.plate_detection_roi_w,
            printer.plate_detection_roi_h,
        )
    res = await check_plate_empty(
        printer_id=printer.id,
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        external_camera_url=printer.external_camera_url,
        external_camera_type=printer.external_camera_type,
        use_external=printer.external_camera_enabled,
        roi=roi,
        external_camera_snapshot_url=printer.external_camera_snapshot_url,
    )
    if res.needs_calibration:
        return False, "camera check needs plate-detection calibration"
    if not res.is_empty:
        return False, f"camera still sees something on the bed ({res.difference_percent:.1f}% different)"
    return True, "camera confirms the bed is empty"


async def run_eject(printer_id: int, profile_id: int, *, sleep=asyncio.sleep, clock=time.monotonic) -> EjectResult:
    """Run one eject. The caller has already put the printer in EJECTING."""
    try:
        async with async_session() as db:
            printer = await db.get(Printer, printer_id)
            profile = await db.get(EjectionProfile, profile_id)
            if printer is None or profile is None:
                return _done(printer_id, False, "printer or profile no longer exists")
            timeout = float(profile.timeout_s or 1800)

            if profile.max_bed_temp_c is not None:
                limit = float(profile.max_bed_temp_c)

                def cooled():
                    t = _bed_temp(printer_id)
                    return t is not None and t <= limit

                if not await _wait_until(cooled, timeout, sleep, clock):
                    return _done(printer_id, False, f"the bed did not cool to {limit:g}C within {int(timeout)}s")

            client = printer_manager.get_client(printer_id)
            if client is None or not getattr(getattr(client, "state", None), "connected", False):
                return _done(printer_id, False, "printer not connected")
            if not hasattr(client, "send_gcode"):
                return _done(printer_id, False, "this printer type can't run G-code")
            if not client.send_gcode(profile.gcode):
                return _done(printer_id, False, "the printer refused the eject G-code")
            logger.info("[EJECT] printer %s: eject sequence sent (profile %s)", printer_id, profile.name)

            await sleep(max(0, int(profile.settle_s or 0)))
            if not await _wait_until(lambda: _state(printer_id) in IDLE_STATES, timeout, sleep, clock):
                return _done(printer_id, False, f"the printer did not return to idle within {int(timeout)}s")

            if profile.verify_with_camera:
                ok, why = await _verify_empty(printer)
                if not ok:
                    return _done(printer_id, False, why)

            printer.consecutive_ejects = (printer.consecutive_ejects or 0) + 1
            await db.commit()
        printer_manager.set_awaiting_plate_clear(printer_id, False)
        return _done(printer_id, True, "ejected; the plate-clear gate was cleared")
    except Exception as e:  # noqa: BLE001 - never let an eject error escape the background task
        logger.exception("[EJECT] printer %s: eject failed", printer_id)
        return _done(printer_id, False, f"eject failed: {e}")
    finally:
        EJECTING.discard(printer_id)


def _done(printer_id: int, ok: bool, reason: str) -> EjectResult:
    prev = LAST_RESULT.get(printer_id)
    res = EjectResult(printer_id=printer_id, ok=ok, reason=reason)
    if prev and not prev.finished_at:
        res.started_at = prev.started_at
    res.finished_at = datetime.now(timezone.utc).isoformat()
    LAST_RESULT[printer_id] = res
    (logger.info if ok else logger.warning)("[EJECT] printer %s: %s", printer_id, reason)
    return res


async def maybe_start_after_print(printer_id: int, status: str) -> str | None:
    """Called from on_print_complete after the gate is raised. Starts an eject
    in the background when the printer is eligible; returns the reason when not."""
    if printer_id in EJECTING:
        return "an eject is already running"
    # Hold the printer as busy while we look it up: the scheduler runs on the same
    # loop and would otherwise see an idle printer during the database await
    # (with require_plate_clear off nothing else stops it).
    EJECTING.add(printer_id)
    try:
        async with async_session() as db:
            printer = await db.get(Printer, printer_id)
            if printer is None:
                EJECTING.discard(printer_id)
                return "printer not found"
            profile = (
                await db.get(EjectionProfile, printer.ejection_profile_id) if printer.ejection_profile_id else None
            )
            why = eligibility(printer, profile, status)
    except Exception:
        EJECTING.discard(printer_id)
        raise
    if why:
        EJECTING.discard(printer_id)
        if getattr(printer, "auto_eject", False):
            logger.info("[EJECT] printer %s: not ejecting: %s", printer_id, why)
            _done(printer_id, False, f"not ejected: {why}")
        return why
    start(printer_id, profile.id)
    return None


def start(printer_id: int, profile_id: int) -> None:
    """Mark the printer EJECTING now, then run the eject as a background task."""
    from backend.app.core.tasks import spawn_background_task

    EJECTING.add(printer_id)
    LAST_RESULT[printer_id] = EjectResult(printer_id=printer_id, ok=False, reason="ejecting")
    spawn_background_task(run_eject(printer_id, profile_id), name=f"eject-{printer_id}")
