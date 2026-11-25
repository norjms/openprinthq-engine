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

Status updates arrive concurrently (a Bambu printer sends several MQTT reports
a second), so ``observe()`` runs one at a time per printer: two calls that both
saw an alarm as new would otherwise insert it twice, and only one of the copies
would ever be cleared. Loading the open rows (at start-up, or after
``forget()``) clears any such extra copy, keeping the oldest.

Notifications: Bambu HMS errors already notify from main.py, so the feed only
notifies (``on_printer_error``) for new fatal or serious alarms from the other
transports, and for PrusaLink ATTENTION.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from backend.app.core import database
from backend.app.models.printer_alarm import AlarmSuppression, PrinterAlarm

logger = logging.getLogger(__name__)

SEVERITIES = ("fatal", "serious", "warning")
# Bambu HMS severity: 1 fatal, 2 serious, 3 common, 4 info (the message level
# bambu_mqtt.hms_level() reads from the high half of an hms[] code).
_HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "warning"}
# HMS codes that are not problems (the printer reports its own cancel as one).
HMS_IGNORE = frozenset({"0500_400E"})

CLEAR_GRACE_S = 30.0
# A code that comes back this soon after clearing reopens its alarm (keeping who
# saw or dismissed it) instead of raising a new one. Some codes come and go for
# hours: an H2D reports a dirty nozzle camera lens for about 2.5 minutes, drops
# it for about one, and reports it again.
REOPEN_WITHIN_S = 15 * 60.0
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
    full_code: str | None = None

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
            out[short] = Alarm("bambu", short, severity, text, full_code.upper() if full_code else None)
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

# printer_id -> {(source, code): [row_id, last_touch_monotonic, missing_since or None, message]}
_open: dict[int, dict[tuple[str, str], list]] = {}
# printer_id -> the (source, code) pairs suppressed on it
_suppressed: dict[int, frozenset[tuple[str, str]]] = {}
_last_prune = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _load(printer_id: int) -> dict[tuple[str, str], list]:
    """The printer's open rows by (source, code). Extra open copies of one alarm are cleared, the oldest kept."""
    async with database.async_session() as db:
        rows = (
            await db.execute(
                select(PrinterAlarm)
                .where(PrinterAlarm.printer_id == printer_id, PrinterAlarm.cleared_at.is_(None))
                .order_by(PrinterAlarm.first_seen, PrinterAlarm.id)
            )
        ).scalars()
        now = _clock()
        out: dict[tuple[str, str], list] = {}
        extra: list[int] = []
        for r in rows:
            if (r.source, r.code) in out:
                extra.append(r.id)
            else:
                out[(r.source, r.code)] = [r.id, now, None, r.message]
        if extra:
            await db.execute(update(PrinterAlarm).where(PrinterAlarm.id.in_(extra)).values(cleared_at=_now()))
            await db.commit()
            logger.info("[ALARM] printer %s: cleared %d duplicate open alarm row(s)", printer_id, len(extra))
        return out


# Per printer, remembering the event loop each lock was made in: a lock left held
# by a task whose loop has gone (a test that ended mid-observe) must not block the
# next loop. The loop itself is kept, since a new loop can reuse a dead one's id().
_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


async def observe(printer_id: int, state, client=None) -> list[Alarm]:
    """Diff the printer's active alarms against the open rows. Returns new alarms."""
    loop = asyncio.get_running_loop()
    held = _locks.get(printer_id)
    if held is None or held[0] is not loop:
        held = _locks[printer_id] = (loop, asyncio.Lock())
    lock = held[1]
    async with lock:
        return await _observe(printer_id, state, client)


async def _observe(printer_id: int, state, client=None) -> list[Alarm]:
    if client is None:
        from backend.app.services.printer_manager import printer_manager

        client = printer_manager.get_client(printer_id)
    source = source_of(client)
    if source is None or not getattr(state, "connected", False):
        return []
    current = {a.key: a for a in active_alarms(state, source)}
    if current:
        muted = await suppressed(printer_id)
        current = {k: a for k, a in current.items() if k not in muted}
    known = _open.get(printer_id)
    if known is None:
        known = _open[printer_id] = await _load(printer_id)
    if not current and not known:
        return []

    mono = _clock()
    new = [a for k, a in current.items() if k not in known]
    touch = [k for k in current if k in known and mono - known[k][1] >= TOUCH_EVERY_S]
    # Text that changed (a catalog update, or a row recorded before the code had text).
    retext = [k for k in current if k in known and known[k][3] != current[k].message]
    gone: list[tuple[str, str]] = []
    for k, entry in known.items():
        if k in current:
            entry[2] = None
        elif entry[2] is None:
            entry[2] = mono
        elif mono - entry[2] >= CLEAR_GRACE_S:
            gone.append(k)

    if new or touch or gone or retext:
        now = _now()
        async with database.async_session() as db:
            rows = []
            reopened: list[Alarm] = []
            for a in new:
                row = await _recently_cleared(db, printer_id, a, now)
                if row is not None:
                    row.cleared_at = None
                    row.last_seen = now
                    row.severity = a.severity
                    row.message = a.message[:2000]
                    row.full_code = a.full_code
                    rows.append((a.key, row))
                    reopened.append(a)
                    continue
                row = PrinterAlarm(
                    printer_id=printer_id,
                    source=a.source,
                    code=a.code[:40],
                    severity=a.severity,
                    message=a.message[:2000],
                    full_code=a.full_code,
                    first_seen=now,
                    last_seen=now,
                )
                db.add(row)
                rows.append((a.key, row))
            if touch:
                await db.execute(
                    update(PrinterAlarm).where(PrinterAlarm.id.in_([known[k][0] for k in touch])).values(last_seen=now)
                )
            for k in retext:
                a = current[k]
                await db.execute(
                    update(PrinterAlarm)
                    .where(PrinterAlarm.id == known[k][0])
                    .values(message=a.message[:2000], full_code=a.full_code)
                )
            if gone:
                await db.execute(
                    update(PrinterAlarm).where(PrinterAlarm.id.in_([known[k][0] for k in gone])).values(cleared_at=now)
                )
            await db.commit()
            for key, row in rows:
                known[key] = [row.id, mono, None, row.message]
        new = [a for a in new if a not in reopened]
        for k in touch:
            known[k][1] = mono
        for k in retext:
            known[k][3] = current[k].message
        for k in gone:
            known.pop(k, None)
    if new:
        logger.warning(
            "[ALARM] printer %s: %s", printer_id, "; ".join(f"{a.severity} {a.code}: {a.message}" for a in new)
        )
        await _notify(printer_id, source, new)
    await _maybe_prune()
    return new


async def _recently_cleared(db, printer_id: int, a: Alarm, now: datetime) -> PrinterAlarm | None:
    """The alarm's own row if the printer cleared it less than REOPEN_WITHIN_S ago."""
    return (
        await db.execute(
            select(PrinterAlarm)
            .where(
                PrinterAlarm.printer_id == printer_id,
                PrinterAlarm.source == a.source,
                PrinterAlarm.code == a.code[:40],
                PrinterAlarm.cleared_at.is_not(None),
                PrinterAlarm.cleared_at >= now - timedelta(seconds=REOPEN_WITHIN_S),
            )
            .order_by(PrinterAlarm.cleared_at.desc(), PrinterAlarm.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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


async def dismiss(alarm_ids: list[int], who: str | None) -> int:
    """Dismiss alarms: they leave the active list (and count as acknowledged) while the printer still reports them.

    The printer clearing the code and reporting it again makes a new alarm.
    """
    if not alarm_ids:
        return 0
    now = _now()
    async with database.async_session() as db:
        res = await db.execute(
            update(PrinterAlarm)
            .where(PrinterAlarm.id.in_(alarm_ids), PrinterAlarm.dismissed_at.is_(None))
            .values(dismissed_at=now, dismissed_by=(who or None))
        )
        await db.execute(
            update(PrinterAlarm)
            .where(PrinterAlarm.id.in_(alarm_ids), PrinterAlarm.acknowledged_at.is_(None))
            .values(acknowledged_at=now, acknowledged_by=(who or None))
        )
        await db.commit()
    return res.rowcount or 0


async def suppressed(printer_id: int) -> frozenset[tuple[str, str]]:
    """The (source, code) pairs nobody wants alerts for on this printer. Cached."""
    out = _suppressed.get(printer_id)
    if out is None:
        async with database.async_session() as db:
            rows = (
                await db.execute(
                    select(AlarmSuppression.source, AlarmSuppression.code).where(
                        AlarmSuppression.printer_id == printer_id
                    )
                )
            ).all()
        out = _suppressed[printer_id] = frozenset((r[0], r[1]) for r in rows)
    return out


async def suppress(printer_id: int, source: str, code: str, message: str | None, who: str | None) -> AlarmSuppression:
    """Stop alerting about one code on one printer, and dismiss its open alarms. Idempotent."""
    async with database.async_session() as db:
        row = (
            await db.execute(
                select(AlarmSuppression).where(
                    AlarmSuppression.printer_id == printer_id,
                    AlarmSuppression.source == source,
                    AlarmSuppression.code == code,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = AlarmSuppression(
                printer_id=printer_id,
                source=source,
                code=code[:40],
                message=(message or None) and message[:2000],
                created_by=(who or None),
                created_at=_now(),
            )
            db.add(row)
        open_ids = (
            (
                await db.execute(
                    select(PrinterAlarm.id).where(
                        PrinterAlarm.printer_id == printer_id,
                        PrinterAlarm.source == source,
                        PrinterAlarm.code == code,
                        PrinterAlarm.cleared_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
        await db.refresh(row)
    _suppressed.pop(printer_id, None)
    await dismiss(list(open_ids), who)
    logger.info("[ALARM] printer %s: %s %s suppressed by %s", printer_id, source, code, who or "someone")
    return row


async def unsuppress(suppression_id: int) -> AlarmSuppression | None:
    """Alert about a suppressed code again. Returns the removed suppression, or None."""
    async with database.async_session() as db:
        row = await db.get(AlarmSuppression, suppression_id)
        if row is None:
            return None
        printer_id = row.printer_id
        await db.delete(row)
        # Alerts are wanted again: the code's recent alarm reopens unseen, not dismissed.
        await db.execute(
            update(PrinterAlarm)
            .where(
                PrinterAlarm.printer_id == printer_id,
                PrinterAlarm.source == row.source,
                PrinterAlarm.code == row.code,
                PrinterAlarm.cleared_at.is_not(None),
                PrinterAlarm.cleared_at >= _now() - timedelta(seconds=REOPEN_WITHIN_S),
            )
            .values(dismissed_at=None, dismissed_by=None, acknowledged_at=None, acknowledged_by=None)
        )
        await db.commit()
    _suppressed.pop(printer_id, None)
    return row


def forget(printer_id: int) -> None:
    """Drop a printer's cached open alarms (printer deleted or reconfigured)."""
    _open.pop(printer_id, None)
    _suppressed.pop(printer_id, None)


def reset() -> None:
    """Tests: clear the in-memory cache."""
    global _last_prune
    _open.clear()
    _suppressed.clear()
    _locks.clear()
    _last_prune = 0.0
