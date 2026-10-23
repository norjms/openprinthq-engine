"""Farm analytics (OpenPrintHQ farm feature). See services/farm_analytics.py."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.services import farm_analytics

router = APIRouter(tags=["stats"])


@router.get("/stats/farm")
async def farm_stats(
    days: int = Query(7, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    """Hours each printer spent printing, paused, waiting for the plate to be
    cleared, idle and offline; utilization (printing out of connected time);
    and queue items completed per day."""
    return await farm_analytics.report(db, days=days)
