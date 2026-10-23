"""Engine-native failure detection (OpenPrintHQ farm feature).

Every source that can tell a print went wrong records a PrintFailureEvent here,
so a farm has one timeline and one policy instead of four separate behaviours:

- ``bambu_hms``: the printer's own AI monitor (spaghetti, nozzle clumping) via HMS
  codes, recorded from the HMS handler in main.py;
- ``obico``: the Obico ML detector, recorded from services/obico_actions.py;
- ``klipper``: a Klipper print that ended in error, with Klipper's message;
- ``stall``: a print whose progress and layer have not moved for
  ``failure_stall_minutes`` while the printer says it is printing (off by default).

Policy: with the ``failure_hold_printer`` setting on, a printer with an
unacknowledged event is held: the queue starts nothing on it until someone
acknowledges the events (POST /failure-events/{id}/ack or
/printers/{id}/failure-events/ack). Detection itself never pauses or stops a
print here; the sources that already do that (the printer's own AI halt, the
Obico action setting) keep their own settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update

from backend.app.core import database
from backend.app.models.failure_event import PrintFailureEvent
from backend.app.models.settings import Settings

logger = logging.getLogger(__name__)

# HMS short codes that are a detection by the printer's AI monitor, not a fault.
BAMBU_DETECTION_CODES: dict[str, str] = {
    "0300_8003": "Spaghetti detected by the printer's AI monitor",
    "0C00_8042": "Spaghetti detected by the printer's AI monitor",
    "0C00_803F": "Nozzle clumping detected by the printer's AI monitor",
    "0C00_8043": "Nozzle clumping detected by the printer's AI monitor",
}

# Printers held after a detection (mirrors unacknowledged events when the hold
# setting is on). Loaded at startup; read synchronously by the scheduler.
HELD: set[int] = set()


def is_held(printer_id: int) -> bool:
    return printer_id in HELD


async def _setting(db, key: str, default: str) -> str:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    return row.value if row and row.value is not None else default


async def hold_enabled(db) -> bool:
    return (await _setting(db, "failure_hold_printer", "false")).lower() == "true"


async def stall_minutes(db) -> int:
    try:
        return max(0, int(await _setting(db, "failure_stall_minutes", "0")))
    except ValueError:
        return 0


async def record_event(
    printer_id: int,
    source: str,
    message: str,
    *,
    code: str | None = None,
    score: float | None = None,
    action_taken: str | None = None,
) -> PrintFailureEvent:
    async with database.async_session() as db:
        hold = await hold_enabled(db)
        ev = PrintFailureEvent(
            printer_id=printer_id,
            source=source,
            code=code,
            message=message[:2000],
            score=score,
            action_taken=(f"{action_taken}+hold" if action_taken else "hold") if hold else action_taken,
        )
        db.add(ev)
        await db.commit()
        await db.refresh(ev)
    if hold:
        HELD.add(printer_id)
    logger.warning("[FAILURE] printer %s: %s: %s%s", printer_id, source, message, " (printer held)" if hold else "")
    return ev


async def acknowledge(event_ids: list[int] | None, printer_id: int | None, who: str | None) -> int:
    """Acknowledge events (by id, or all of a printer's). Releases the hold on any
    printer left with no unacknowledged events. Returns how many were acknowledged."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with database.async_session() as db:
        q = update(PrintFailureEvent).where(PrintFailureEvent.acknowledged_at.is_(None))
        if event_ids is not None:
            q = q.where(PrintFailureEvent.id.in_(event_ids))
        if printer_id is not None:
            q = q.where(PrintFailureEvent.printer_id == printer_id)
        res = await db.execute(q.values(acknowledged_at=now, acknowledged_by=(who or None)))
        await db.commit()
        still = set(
            (await db.execute(select(PrintFailureEvent.printer_id).where(PrintFailureEvent.acknowledged_at.is_(None))))
            .scalars()
            .all()
        )
    for pid in list(HELD):
        if pid not in still:
            HELD.discard(pid)
    return res.rowcount or 0


async def load_holds() -> None:
    """At startup: hold printers that still have unacknowledged events (when on)."""
    async with database.async_session() as db:
        if not await hold_enabled(db):
            HELD.clear()
            return
        ids = (
            await db.execute(select(PrintFailureEvent.printer_id).where(PrintFailureEvent.acknowledged_at.is_(None)))
        ).scalars()
        HELD.clear()
        HELD.update(set(ids))


# ---------------------------------------------------------------- feeds


async def on_bambu_hms(printer_id: int, short_codes: list[str]) -> list[PrintFailureEvent]:
    out = []
    for code in short_codes:
        if code in BAMBU_DETECTION_CODES:
            out.append(await record_event(printer_id, "bambu_hms", BAMBU_DETECTION_CODES[code], code=code))
    return out


class StallDetector:
    """Fires once per print when progress and layer stop moving while RUNNING.

    ``observe`` is fed the printer state (from the status callback or a timer);
    the clock is injectable for tests.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._seen: dict[int, tuple[object, float, bool]] = {}  # pid -> (marker, since, fired)

    def observe(self, printer_id: int, state, minutes: int) -> bool:
        """Returns True exactly when a stall should be recorded."""
        if not minutes or getattr(state, "state", None) != "RUNNING":
            self._seen.pop(printer_id, None)
            return False
        marker = (getattr(state, "progress", None), getattr(state, "layer_num", None))
        now = self.clock()
        prev = self._seen.get(printer_id)
        if prev is None or prev[0] != marker:
            self._seen[printer_id] = (marker, now, False)
            return False
        if prev[2] or now - prev[1] < minutes * 60:
            return False
        self._seen[printer_id] = (marker, prev[1], True)
        return True


stall_detector = StallDetector()


async def stall_loop(interval_s: float = 60.0) -> None:
    """Background task: checks every printer once a minute."""
    from backend.app.services.printer_manager import printer_manager

    while True:
        try:
            async with database.async_session() as db:
                minutes = await stall_minutes(db)
            if minutes:
                for pid in list(getattr(printer_manager, "_clients", {}) or {}):
                    st = printer_manager.get_status(pid)
                    if st and stall_detector.observe(pid, st, minutes):
                        await record_event(
                            pid,
                            "stall",
                            f"No progress for {minutes} minutes while printing "
                            f"(progress {getattr(st, 'progress', '?')}%, layer {getattr(st, 'layer_num', '?')})",
                        )
                        await _notify(pid, f"Print stalled: no progress for {minutes} minutes")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[FAILURE] stall check failed: %s", e)
        await asyncio.sleep(interval_s)


async def _notify(printer_id: int, detail: str) -> None:
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    try:
        async with database.async_session() as db:
            printer = await db.get(Printer, printer_id)
            name = printer.name if printer else f"Printer {printer_id}"
            await notification_service.on_printer_error(printer_id, name, "Print failure detected", db, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("[FAILURE] notification failed for printer %s: %s", printer_id, e)
