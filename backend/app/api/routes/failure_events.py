"""Detected print failures and the hold policy (OpenPrintHQ farm feature).

See services/failure_detection.py.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.failure_event import PrintFailureEvent
from backend.app.models.settings import Settings
from backend.app.services import failure_detection

router = APIRouter(tags=["failure-detection"])


class FailureEventOut(BaseModel):
    id: int
    printer_id: int
    source: str
    code: str | None
    message: str
    score: float | None
    action_taken: str | None
    created_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by: str | None

    class Config:
        from_attributes = True


class FailurePolicy(BaseModel):
    hold_printer: bool = False
    stall_minutes: int = Field(default=0, ge=0, le=1440)


class AckBody(BaseModel):
    by: str | None = Field(default=None, max_length=100)


@router.get("/failure-events", response_model=list[FailureEventOut])
async def list_events(
    printer_id: int | None = None,
    unacknowledged: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    q = select(PrintFailureEvent).order_by(PrintFailureEvent.id.desc()).limit(limit)
    if printer_id is not None:
        q = q.where(PrintFailureEvent.printer_id == printer_id)
    if unacknowledged:
        q = q.where(PrintFailureEvent.acknowledged_at.is_(None))
    return (await db.execute(q)).scalars().all()


@router.post("/failure-events/{event_id}/ack")
async def ack_event(
    event_id: int,
    body: AckBody | None = None,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    n = await failure_detection.acknowledge([event_id], None, body.by if body else None)
    return {"acknowledged": n}


@router.post("/printers/{printer_id}/failure-events/ack")
async def ack_printer(
    printer_id: int,
    body: AckBody | None = None,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    """Acknowledge every open event on a printer, releasing the hold on it."""
    n = await failure_detection.acknowledge(None, printer_id, body.by if body else None)
    return {"acknowledged": n, "held": failure_detection.is_held(printer_id)}


@router.get("/failure-detection/policy", response_model=FailurePolicy)
async def get_policy(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    return FailurePolicy(
        hold_printer=await failure_detection.hold_enabled(db),
        stall_minutes=await failure_detection.stall_minutes(db),
    )


@router.put("/failure-detection/policy", response_model=FailurePolicy)
async def put_policy(
    body: FailurePolicy,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    for key, value in (
        ("failure_hold_printer", "true" if body.hold_printer else "false"),
        ("failure_stall_minutes", str(body.stall_minutes)),
    ):
        row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(Settings(key=key, value=value))
    await db.commit()
    await failure_detection.load_holds()
    return body
