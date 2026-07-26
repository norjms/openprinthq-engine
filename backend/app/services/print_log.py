"""Service for writing independent print log entries.

Log entries are written to a separate table and never touch archives or queue items.
"""

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_log import PrintLogEntry

logger = logging.getLogger(__name__)


async def write_log_entry(
    db: AsyncSession,
    *,
    status: str,
    archive_id: int | None = None,
    print_name: str | None = None,
    printer_name: str | None = None,
    printer_id: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    filament_type: str | None = None,
    filament_color: str | None = None,
    filament_used_grams: float | None = None,
    cost: float | None = None,
    energy_kwh: float | None = None,
    energy_cost: float | None = None,
    failure_reason: str | None = None,
    thumbnail_path: str | None = None,
    created_by_id: int | None = None,
    created_by_username: str | None = None,
) -> PrintLogEntry:
    """Write a print log entry."""
    duration = None
    if started_at and completed_at:
        duration = int((completed_at - started_at).total_seconds())

    entry = PrintLogEntry(
        archive_id=archive_id,
        print_name=print_name,
        printer_name=printer_name,
        printer_id=printer_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        filament_type=filament_type,
        filament_color=filament_color,
        filament_used_grams=filament_used_grams,
        cost=cost,
        energy_kwh=energy_kwh,
        energy_cost=energy_cost,
        failure_reason=failure_reason,
        thumbnail_path=thumbnail_path,
        created_by_id=created_by_id,
        created_by_username=created_by_username,
    )
    db.add(entry)
    await db.flush()
    return entry


async def compute_entry_cost(db, filament_grams, filament_type, duration_seconds):
    """Best-effort per-entry cost. Filament cost is exact (grams x the spool's
    cost_per_kg, else the default_filament_cost setting). Energy is an estimate
    (duration x nominal draw x the energy rate) until a smart plug supplies a
    real reading. Never raises — returns (filament_cost, energy_kwh, energy_cost),
    any of which may be None."""
    cost = energy_kwh = energy_cost = None
    try:
        from backend.app.api.routes.settings import get_setting
        if filament_grams:
            cpk = None
            try:
                if filament_type:
                    from sqlalchemy import select
                    from backend.app.models.filament import Filament
                    primary = str(filament_type).split(",")[0].strip()
                    fil = (await db.execute(select(Filament).where(Filament.type == primary).limit(1))).scalar_one_or_none()
                    if fil and getattr(fil, "cost_per_kg", None):
                        cpk = float(fil.cost_per_kg)
            except Exception:
                cpk = None
            if cpk is None:
                dc = await get_setting(db, "default_filament_cost")
                cpk = float(dc) if dc else 25.0
            cost = round((float(filament_grams) / 1000.0) * cpk, 2)
        if duration_seconds:
            rate_s = await get_setting(db, "energy_cost_per_kwh")
            rate = float(rate_s) if rate_s else 0.15
            NOMINAL_KW = 0.10  # avg FDM draw; replace with smart-plug reading when present
            energy_kwh = round((float(duration_seconds) / 3600.0) * NOMINAL_KW, 3)
            energy_cost = round(energy_kwh * rate, 2)
    except Exception:
        pass
    return cost, energy_kwh, energy_cost
