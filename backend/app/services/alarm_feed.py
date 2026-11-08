"""Unified alarm feed (OpenPrintHQ farm feature).

One list of the problems every printer is reporting, whatever the transport:

- ``bambu``: HMS codes, severity fatal, serious or common (info and Bambu's
  internal codes are left out), described from the bundled HMS catalog;
- ``klipper``: a print that ended in error with Klipper's message, and a Klippy
  shutdown or disconnect;
- ``prusalink``: ATTENTION (filament runout, MMU, a detected crash: the printer
  waits for someone) and ERROR;
- ``octoprint``, ``duet``, ``flashforge``, ``mks``, ``snapmaker``: the printer's
  error state.

``observe()`` runs on every printer status change. It works out the printer's
active alarms (``active_alarms()``, pure) and diffs them against the open rows:
a new alarm is inserted, one still reported has ``last_seen`` refreshed (at most
once a minute), and one no longer reported is cleared after a short grace, so a
code that flaps on and off (Bambu chamber temperature warnings do) stays one
row. Nothing is cleared while the printer is disconnected: no news is not good
news. Rows are kept for ``RETENTION_DAYS`` after they clear.

Notifications: Bambu HMS errors already notify from main.py, so the feed only
notifies (``on_printer_error``) for new fatal or serious alarms from the other
transports, and for PrusaLink ATTENTION.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from backend.app.core import database
from backend.app.models.printer_alarm import PrinterAlarm

logger = logging.getLogger(__name__)

SEVERITIES = ("fatal", "serious", "warning")
# Bambu HMS severity: 1 fatal, 2 serious, 3 common, 4 info (the message level
# bambu_mqtt.hms_level() reads from the high half of an hms[] code).
_HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "warning"}
# HMS codes that are not problems (the printer reports its own cancel as one).
HMS_IGNORE = frozenset({"0500_400E"})

CLEAR_GRACE_S = 30.0
TOUCH_EVERY_S = 60.0
RETENTION_DAYS = 90
_PRUNE_EVERY_S = 6 * 3600.0


def _clock() -> float:
    """Monotonic seconds; tests replace this, never time.monotonic itself."""
    return time.monotonic()


_CLASS_SOURCE = {
    "BambuMQTTClient": "bambu",
    "MoonrakerClient": "klipper",
    "DuetClient": "duet",
    "FlashForgeClient": "flashforge",
    "MKSClient": "mks",
    "SnapmakerClient": "snapmaker",
    "ElegooSDCPClient": "elegoo",
    "ElegooCC2Client": "elegoo_cc2",
    "AnycubicClient": "anycubic",
    "ObicoClient": "obico",
}


@dataclass(frozen=True)
class Alarm:
    source: str
    code: str
    severity: str
    message: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.code)


def source_of(client) -> str | None:
    """Which transport a printer client is (None for an unknown object)."""
    if client is None:
        return None
    flavor = getattr(client, "flavor", None)
    if flavor:
        return str(flavor)
    return _CLASS_SOURCE.get(type(client).__name__)


def hms_short_code(err) -> str:
    """The catalog code (MMMM_EEEE) of a Bambu HMS error."""
    code_int = int(str(err.code).replace("0x", ""), 16) if err.code else 0
    return f"{(int(err.attr) >> 16) & 0xFFFF:04X}_{code_int & 0xFFFF:04X}"


def _hms_alarms(state) -> list[Alarm]:
    from backend.app.services.hms_errors import get_error_description, is_internal_code

    out: dict[str, Alarm] = {}
    for err in getattr(state, "hms_errors", None) or []:
        severity = _HMS_SEVERITY.get(int(getattr(err, "severity", 4) or 4))
        if severity is None:
            continue
        try:
            short = hms_short_code(err)
        except (TypeError, ValueError):
            continue
        if short in HMS_IGNORE:
            continue
        full_code = getattr(err, "full_code", None) or None
        # Internal codes (no published text) are not shown by Bambu Studio either.
        if is_internal_code(full_code):
            continue
        text = get_error_description(short, full_code) or getattr(err, "message", "") or f"HMS {short}"
        prev = out.get(short)
        if prev is None or SEVERITIES.index(severity) < SEVERITIES.index(prev.severity):
            out[short] = Alarm("bambu", short, severity, text)
    return list(out.values())


def active_alarms(state, source: str | None) -> list[Alarm]:
    """The alarms a printer state shows right now. Pure: no I/O."""
    if state is None or source in (None, "obico"):
        return []
    if source == "bambu":
        return _hms_alarms(state)
    raw = getattr(state, "raw_data", None) or {}
    gcode_state = str(getattr(state, "state", "") or "").upper()
    alarms: list[Alarm] = []
    if source == "klipper":
        klippy = raw.get("klippy_alarm")
        if klippy:
            alarms.append(
                Alarm("klipper", f"klippy_{klippy}", "fatal", f"Klipper reports {klippy}: the firmware is not running")
            )
        message = (raw.get("klipper_message") or "").strip()
        if gcode_state == "FAILED" and message:
            alarms.append(Alarm("klipper", "print_error", "serious", message))
        return alarms
    if source == "prusalink":
        text = str(raw.get("printer_state_text") or "").upper()
        if text == "ATTENTION":
            alarms.append(
                Alarm(
                    "prusalink",
                    "attention",
                    "warning",
                    "The printer needs attention (filament runout, MMU or a detected crash); resume it at the printer",
                )
            )
        elif text == "ERROR" or gcode_state == "FAILED":
            alarms.append(Alarm("prusalink", "printer_error", "serious", "The printer reports an error"))
        return alarms
    if gcode_state == "FAILED":
        alarms.append(Alarm(source, "printer_error", "serious", "The printer reports an error"))
    return alarms


# ---------------------------------------------------------------- state

# printer_id -> {(source, code): [row_id, last_touch_monotonic, missing_since or None]}
_open: dict[int, dict[tuple[str, str], list]] = {}
_last_prune = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _load(printer_id: int) -> dict[tuple[str, str], list]:
    async with database.async_session() as db:
        rows = (
            await db.execute(
                select(PrinterAlarm).where(PrinterAlarm.printer_id == printer_id, PrinterAlarm.cleared_at.is_(None))
            )
        ).scalars()
        now = _clock()
        return {(r.source, r.code): [r.id, now, None] for r in rows}


async def observe(printer_id: int, state, client=None) -> list[Alarm]:
    """Diff the printer's active alarms against the open rows. Returns new alarms."""
    if client is None:
        from backend.app.services.printer_manager import printer_manager

        client = printer_manager.get_client(printer_id)
    source = source_of(client)
    if source is None or not getattr(state, "connected", False):
        return []
    current = {a.key: a for a in active_alarms(state, source)}
    known = _open.get(printer_id)
    if known is None:
        known = _open[printer_id] = await _load(printer_id)
    if not current and not known:
        return []

    mono = _clock()
    new = [a for k, a in current.items() if k not in known]
    touch = [k for k in current if k in known and mono - known[k][1] >= TOUCH_EVERY_S]
    gone: list[tuple[str, str]] = []
    for k, entry in known.items():
        if k in current:
            entry[2] = None
        elif entry[2] is None:
            entry[2] = mono
        elif mono - entry[2] >= CLEAR_GRACE_S:
            gone.append(k)

    if new or touch or gone:
        now = _now()
        async with database.async_session() as db:
            rows = []
            for a in new:
                row = PrinterAlarm(
                    printer_id=printer_id,
                    source=a.source,
                    code=a.code[:40],
                    severity=a.severity,
                    message=a.message[:2000],
                    first_seen=now,
                    last_seen=now,
                )
                db.add(row)
                rows.append((a.key, row))
            if touch:
                await db.execute(
                    update(PrinterAlarm).where(PrinterAlarm.id.in_([known[k][0] for k in touch])).values(last_seen=now)
                )
            if gone:
                await db.execute(
                    update(PrinterAlarm).where(PrinterAlarm.id.in_([known[k][0] for k in gone])).values(cleared_at=now)
                )
            await db.commit()
            for key, row in rows:
                known[key] = [row.id, mono, None]
        for k in touch:
            known[k][1] = mono
        for k in gone:
            known.pop(k, None)
    if new:
        logger.warning(
            "[ALARM] printer %s: %s", printer_id, "; ".join(f"{a.severity} {a.code}: {a.message}" for a in new)
        )
        await _notify(printer_id, source, new)
    await _maybe_prune()
    return new


async def _notify(printer_id: int, source: str, new: list[Alarm]) -> None:
    if source == "bambu":
        return  # main.py's HMS handler already notifies
    worth = [a for a in new if a.severity in ("fatal", "serious") or (a.source, a.code) == ("prusalink", "attention")]
    if not worth:
        return
    from backend.app.models.printer import Printer
    from backend.app.services.notification_service import notification_service

    try:
        async with database.async_session() as db:
            printer = await db.get(Printer, printer_id)
            name = printer.name if printer else f"Printer {printer_id}"
            for a in worth:
                await notification_service.on_printer_error(printer_id, name, "Printer alarm", db, a.message)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ALARM] notification failed for printer %s: %s", printer_id, e)


async def _maybe_prune() -> None:
    global _last_prune
    mono = _clock()
    if _last_prune and mono - _last_prune < _PRUNE_EVERY_S:
        return
    _last_prune = mono
    try:
        cutoff = _now() - timedelta(days=RETENTION_DAYS)
        async with database.async_session() as db:
            await db.execute(
                delete(PrinterAlarm).where(PrinterAlarm.cleared_at.is_not(None), PrinterAlarm.cleared_at < cutoff)
            )
            await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("[ALARM] prune failed: %s", e)


async def acknowledge(alarm_ids: list[int] | None, printer_id: int | None, who: str | None) -> int:
    """Acknowledge alarms by id, or every unacknowledged alarm of a printer."""
    async with database.async_session() as db:
        q = update(PrinterAlarm).where(PrinterAlarm.acknowledged_at.is_(None))
        if alarm_ids is not None:
            q = q.where(PrinterAlarm.id.in_(alarm_ids))
        if printer_id is not None:
            q = q.where(PrinterAlarm.printer_id == printer_id)
        res = await db.execute(q.values(acknowledged_at=_now(), acknowledged_by=(who or None)))
        await db.commit()
    return res.rowcount or 0


def forget(printer_id: int) -> None:
    """Drop a printer's cached open alarms (printer deleted or reconfigured)."""
    _open.pop(printer_id, None)


def reset() -> None:
    """Tests: clear the in-memory cache."""
    global _last_prune
    _open.clear()
    _last_prune = 0.0
