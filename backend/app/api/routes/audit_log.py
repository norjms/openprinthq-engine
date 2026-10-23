"""Audit log reader (OpenPrintHQ farm feature). See services/audit.py."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.audit_log import AuditLogEntry

router = APIRouter(tags=["audit"])


class AuditLogOut(BaseModel):
    id: int
    ts: datetime | None
    actor: str | None
    actor_kind: str
    ip: str | None
    method: str
    route: str | None
    path: str
    status: int
    duration_ms: int | None

    class Config:
        from_attributes = True


@router.get("/audit-log", response_model=list[AuditLogOut])
async def audit_log(
    actor: str | None = None,
    path_contains: str | None = Query(default=None, max_length=200),
    method: str | None = None,
    since: datetime | None = None,
    before_id: int | None = None,
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Newest first. Page with ``before_id`` (the last id of the previous page)."""
    q = select(AuditLogEntry).order_by(AuditLogEntry.id.desc()).limit(limit)
    if actor:
        q = q.where(AuditLogEntry.actor == actor)
    if path_contains:
        q = q.where(AuditLogEntry.path.contains(path_contains))
    if method:
        q = q.where(AuditLogEntry.method == method.upper())
    if since:
        q = q.where(AuditLogEntry.ts >= since.replace(tzinfo=None))
    if before_id:
        q = q.where(AuditLogEntry.id < before_id)
    return (await db.execute(q)).scalars().all()
