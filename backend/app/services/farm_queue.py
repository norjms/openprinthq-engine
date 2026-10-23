"""Farm queue helpers: automatic retry of failed prints, and queue statistics."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

# Columns a retry does NOT copy: identity, lifecycle and per-attempt state.
_NOT_COPIED = frozenset(
    {
        "id",
        "status",
        "position",
        "waiting_reason",
        "started_at",
        "completed_at",
        "error_message",
        "created_at",
        "dispatching_at",
        "dispatch_attempts",
        "gate_acknowledged",
        "filament_short",
        "been_jumped",
        "retry_count",
        "retry_of_id",
    }
)


async def requeue_failed(db: AsyncSession, item: PrintQueueItem) -> PrintQueueItem | None:
    """When a failed item still has retries left, queue a fresh copy at the front.

    The failed row stays as it is, so the failure keeps its place in history (and an
    integration that reads finished items sees it). The copy carries everything that
    defines the job, counts the attempt in ``retry_count`` and points back with
    ``retry_of_id``. A job pinned to one printer stays pinned; a job aimed at a model or
    group may run on any member, which is usually what you want after a failure.
    The scheduler still waits for the plate to be cleared before the next start.
    Does not commit.
    """
    max_retries = item.max_retries or 0
    done = item.retry_count or 0
    if item.status != "failed" or max_retries <= 0 or done >= max_retries:
        return None
    data = {
        col.key: getattr(item, col.key)
        for col in PrintQueueItem.__table__.columns
        if col.key not in _NOT_COPIED and hasattr(item, col.key)
    }
    if item.target_model or item.target_group_id:
        data["printer_id"] = None
    first = (
        await db.execute(
            select(PrintQueueItem.position)
            .where(PrintQueueItem.status == "pending")
            .order_by(PrintQueueItem.position)
            .limit(1)
        )
    ).scalar()
    copy = PrintQueueItem(
        **data,
        status="pending",
        position=(first if first is not None else 1) - 1,
        retry_count=done + 1,
        retry_of_id=item.retry_of_id or item.id,
    )
    db.add(copy)
    await db.flush()
    logger.info("Queue item %s failed; retry %s/%s queued as %s", item.id, done + 1, max_retries, copy.id)
    return copy


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def queue_stats(db: AsyncSession, days: int = 30) -> dict:
    """Throughput, success rate, waiting and print times over the last ``days`` days,
    overall and per printer. Counts items created in the window."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        (await db.execute(select(PrintQueueItem).where(PrintQueueItem.created_at >= since.replace(tzinfo=None))))
        .scalars()
        .all()
    )
    by_status: dict[str, int] = {}
    per_printer: dict[int, dict] = {}
    waits: list[float] = []
    prints: list[float] = []
    retries = 0
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.retry_count:
            retries += 1
        created, started, ended = _aware(r.created_at), _aware(r.started_at), _aware(r.completed_at)
        if created and started and started >= created:
            waits.append((started - created).total_seconds())
        if r.status == "completed" and started and ended and ended >= started:
            prints.append((ended - started).total_seconds())
        if r.printer_id is not None and r.status in ("completed", "failed", "cancelled"):
            p = per_printer.setdefault(
                r.printer_id, {"printer_id": r.printer_id, "completed": 0, "failed": 0, "cancelled": 0}
            )
            p[r.status] += 1

    def _avg(xs: list[float]) -> int | None:
        return int(sum(xs) / len(xs)) if xs else None

    def _rate(c: int, f: int) -> float | None:
        return round(c / (c + f), 3) if (c + f) else None

    for p in per_printer.values():
        p["success_rate"] = _rate(p["completed"], p["failed"])
    return {
        "days": days,
        "total": len(rows),
        "by_status": by_status,
        "success_rate": _rate(by_status.get("completed", 0), by_status.get("failed", 0)),
        "avg_wait_seconds": _avg(waits),
        "avg_print_seconds": _avg(prints),
        "retried_items": retries,
        "printers": sorted(per_printer.values(), key=lambda p: p["printer_id"]),
    }
