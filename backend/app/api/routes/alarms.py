"""Unified alarm feed: what every printer is reporting (OpenPrintHQ farm feature).

See services/alarm_feed.py and docs/alarm-feed.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import (
    PrinterScope,
    column_in_scope,
    get_printer_scope,
    is_printer_visible,
    visible_printer_ids,
)
from backend.app.models.printer import Printer
from backend.app.models.printer_alarm import PrinterAlarm
from backend.app.services import alarm_feed

router = APIRouter(tags=["alarms"])

Severity = Literal["fatal", "serious", "warning"]


class AlarmOut(BaseModel):
    id: int
    printer_id: int
    printer_name: str | None = None
    source: str
    code: str
    severity: str
    message: str
    first_seen: datetime | None
    last_seen: datetime | None
    cleared_at: datetime | None
    active: bool
    acknowledged_at: datetime | None
    acknowledged_by: str | None


class AlarmSummary(BaseModel):
    active: int
    unacknowledged: int
    by_severity: dict[str, int]
    printers: list[int]


class AckBody(BaseModel):
    by: str | None = Field(default=None, max_length=100)


_RANK = case({"fatal": 0, "serious": 1, "warning": 2}, value=PrinterAlarm.severity, else_=3)


def _out(row: PrinterAlarm, names: dict[int, str]) -> AlarmOut:
    return AlarmOut(
        id=row.id,
        printer_id=row.printer_id,
        printer_name=names.get(row.printer_id),
        source=row.source,
        code=row.code,
        severity=row.severity,
        message=row.message,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        cleared_at=row.cleared_at,
        active=row.cleared_at is None,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
    )


@router.get("/alarms", response_model=list[AlarmOut])
async def list_alarms(
    state: Literal["active", "cleared", "all"] = "active",
    printer_id: int | None = None,
    severity: list[Severity] | None = Query(None, description="Repeat to allow several"),
    unacknowledged: bool = False,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """The farm's alarms. Active ones come worst first, then newest; cleared and all come newest first."""
    q = select(PrinterAlarm)
    in_scope = column_in_scope(PrinterAlarm.printer_id, await visible_printer_ids(db, scope))
    if in_scope is not None:
        q = q.where(in_scope)
    if state == "active":
        q = q.where(PrinterAlarm.cleared_at.is_(None)).order_by(_RANK, PrinterAlarm.first_seen.desc())
    else:
        if state == "cleared":
            q = q.where(PrinterAlarm.cleared_at.is_not(None))
        q = q.order_by(PrinterAlarm.first_seen.desc(), PrinterAlarm.id.desc())
    if printer_id is not None:
        q = q.where(PrinterAlarm.printer_id == printer_id)
    if severity:
        q = q.where(PrinterAlarm.severity.in_(severity))
    if unacknowledged:
        q = q.where(PrinterAlarm.acknowledged_at.is_(None))
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    ids = {r.printer_id for r in rows}
    names = {}
    if ids:
        names = dict((await db.execute(select(Printer.id, Printer.name).where(Printer.id.in_(ids)))).all())
    return [_out(r, names) for r in rows]


@router.get("/alarms/summary", response_model=AlarmSummary)
async def alarm_summary(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """Counts for a badge: active alarms, unacknowledged ones, by severity, and which printers have any."""
    q = select(PrinterAlarm.printer_id, PrinterAlarm.severity, PrinterAlarm.acknowledged_at).where(
        PrinterAlarm.cleared_at.is_(None)
    )
    in_scope = column_in_scope(PrinterAlarm.printer_id, await visible_printer_ids(db, scope))
    if in_scope is not None:
        q = q.where(in_scope)
    rows = (await db.execute(q)).all()
    by = dict.fromkeys(alarm_feed.SEVERITIES, 0)
    for _pid, sev, _ack in rows:
        by[sev] = by.get(sev, 0) + 1
    return AlarmSummary(
        active=len(rows),
        unacknowledged=sum(1 for r in rows if r[2] is None),
        by_severity=by,
        printers=sorted({r[0] for r in rows}),
    )


@router.post("/alarms/{alarm_id}/ack")
async def ack_alarm(
    alarm_id: int,
    body: AckBody | None = None,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """Acknowledge one alarm. It stays in the feed until the printer stops reporting it."""
    row = await db.get(PrinterAlarm, alarm_id)
    if row is None or not await is_printer_visible(db, scope, row.printer_id):
        raise HTTPException(404, "Alarm not found")
    n = await alarm_feed.acknowledge([alarm_id], None, body.by if body else None)
    return {"acknowledged": n}


@router.post("/printers/{printer_id}/alarms/ack")
async def ack_printer_alarms(
    printer_id: int,
    body: AckBody | None = None,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    """Acknowledge every unacknowledged alarm on a printer."""
    n = await alarm_feed.acknowledge(None, printer_id, body.by if body else None)
    return {"acknowledged": n}


@router.get("/alarms/{alarm_id}", response_model=AlarmOut)
async def get_alarm(
    alarm_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    row = await db.get(PrinterAlarm, alarm_id)
    if row is None or not await is_printer_visible(db, scope, row.printer_id):
        raise HTTPException(404, "Alarm not found")
    printer = await db.get(Printer, row.printer_id)
    return _out(row, {row.printer_id: printer.name} if printer else {})
