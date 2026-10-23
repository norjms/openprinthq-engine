"""Filament reconciliation report (OpenPrintHQ farm feature).

See services/filament_reconciliation.py.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.services import filament_reconciliation

router = APIRouter(tags=["inventory"])


@router.get("/inventory/reconciliation")
async def reconciliation(
    days: int = Query(90, ge=1, le=3650),
    flag_pct: float = Query(5.0, ge=0, le=100),
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    """Scale readings against tracked usage: every weighing with the remaining
    weight inventory expected and what the scale measured, totals per material,
    and the weighings whose drift exceeds ``flag_pct`` percent of the label weight."""
    return await filament_reconciliation.report(db, days=days, flag_pct=flag_pct)
