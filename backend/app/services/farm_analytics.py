"""Farm analytics: how printers spend their time (OpenPrintHQ farm feature).

Each printer status update is reduced to one of five buckets and an interval
is written when the bucket changes, so the table grows with state changes,
not with status traffic:

- ``printing``: running, preparing or slicing on the printer
- ``paused``
- ``waiting_plate``: idle but the plate-clear gate is up (a person is needed)
- ``idle``: idle and free for the next job
- ``offline``: not connected

``GET /api/v1/stats/farm?days=7`` reports, per printer and for the farm, the
hours in each bucket, utilization (printing out of connected time) and the
queue items completed per day. Intervals older than a year are dropped.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from backend.app.core import database
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_state_interval import PrinterStateInterval

BUCKETS = ("printing", "paused", "waiting_plate", "idle", "offline")
RETENTION_DAYS = 365

# printer_id -> (bucket, since)
_current: dict[int, tuple[str, datetime]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def bucket_of(state, awaiting_plate: bool) -> str:
    if state is None or not getattr(state, "connected", True):
        return "offline"
    s = str(getattr(state, "state", "") or "").upper()
    if s in ("RUNNING", "PREPARE", "SLICING", "PRINTING"):
        return "printing"
    if s in ("PAUSE", "PAUSED"):
        return "paused"
    if s in ("IDLE", "FINISH", "FAILED"):
        return "waiting_plate" if awaiting_plate else "idle"
    return "offline"


async def observe(printer_id: int, state, awaiting_plate: bool, now: datetime | None = None) -> None:
    """Feed one status update. Writes the finished interval when the bucket changes."""
    b = bucket_of(state, awaiting_plate)
    now = now or _now()
    cur = _current.get(printer_id)
    if cur and cur[0] == b:
        return
    _current[printer_id] = (b, now)
    if cur and (now - cur[1]).total_seconds() >= 1:
        async with database.async_session() as db:
            db.add(PrinterStateInterval(printer_id=printer_id, state=cur[0], started_at=cur[1], ended_at=now))
            if now.minute == 0 and now.second < 5:  # cheap, roughly hourly
                await db.execute(
                    delete(PrinterStateInterval).where(
                        PrinterStateInterval.ended_at < now - timedelta(days=RETENTION_DAYS)
                    )
                )
            await db.commit()


async def report(db, days: int = 7, now: datetime | None = None) -> dict:
    now = now or _now()
    since = now - timedelta(days=days)
    rows = (await db.execute(select(PrinterStateInterval).where(PrinterStateInterval.ended_at > since))).scalars().all()
    secs: dict[int, dict[str, float]] = defaultdict(lambda: dict.fromkeys(BUCKETS, 0.0))

    def add(pid, state, start, end):
        start, end = max(start, since), min(end, now)
        if end > start and state in BUCKETS:
            secs[pid][state] += (end - start).total_seconds()

    for r in rows:
        add(r.printer_id, r.state, r.started_at, r.ended_at)
    for pid, (b, start) in _current.items():
        add(pid, b, start, now)

    def summary(s: dict[str, float]) -> dict:
        hours = {k: round(v / 3600, 2) for k, v in s.items()}
        connected = sum(v for k, v in s.items() if k != "offline")
        return {
            "hours": hours,
            "utilization": round(s["printing"] / connected, 3) if connected else None,
        }

    farm = dict.fromkeys(BUCKETS, 0.0)
    printers = []
    for pid in sorted(secs):
        for k, v in secs[pid].items():
            farm[k] += v
        printers.append({"printer_id": pid, **summary(secs[pid])})

    per_day = defaultdict(int)
    done = (
        await db.execute(
            select(func.date(PrintQueueItem.completed_at), func.count())
            .where(PrintQueueItem.status == "completed", PrintQueueItem.completed_at >= since)
            .group_by(func.date(PrintQueueItem.completed_at))
        )
    ).all()
    for d, n in done:
        per_day[str(d)] = n
    start_day: date = since.date()
    completed_per_day = [
        {"date": str(start_day + timedelta(days=i)), "completed": per_day.get(str(start_day + timedelta(days=i)), 0)}
        for i in range(days + 1)
    ]
    return {"days": days, "farm": summary(farm), "printers": printers, "completed_per_day": completed_per_day}
