"""Filament accounting for non-Bambu printers (Klipper today).

Bambu prints are charged to spools by ``usage_tracker`` (built-in inventory) or
``spoolman_tracking`` (Spoolman mode), both driven off the 3MF archive and the
AMS mapping. Neither can run for an external printer: there is no archive and
no AMS, and ``on_print_complete`` returns before reaching them. So a Klipper
printer used to consume filament without any spool ever being charged.

What an external printer does give us is the extruded length of the job
(Moonraker's ``print_stats.filament_used``, in mm). That is the actual amount
fed, including for a cancelled or failed job, so it is charged as-is.

Which spool: the one assigned to the printer's single feed slot. External
printers have no AMS, so the binding uses the external-spool slot
(``ams_id=255, tray_id=0``). If the printer has exactly one assignment on some
other slot, that one is used instead, so a binding made from a UI that picked a
different slot number still works.

Both inventory modes are handled here so they stay in step.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

logger = logging.getLogger(__name__)

EXTERNAL_AMS_ID = 255
EXTERNAL_TRAY_ID = 0
DEFAULT_DIAMETER_MM = 1.75

_DENSITIES = {
    "PLA-CF": 1.29,
    "PLA": 1.24,
    "PETG": 1.27,
    "PET": 1.38,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA-CF": 1.20,
    "PA": 1.14,
    "PC": 1.20,
    "PVA": 1.23,
    "HIPS": 1.04,
    "PP": 0.90,
}


def density_for(material: str | None) -> float:
    """Typical density in g/cm3; PLA when unknown. Longest prefix wins (PLA-CF before PLA)."""
    mat = (material or "").strip().upper()
    for key in sorted(_DENSITIES, key=len, reverse=True):
        if mat == key or mat.startswith(key):
            return _DENSITIES[key]
    return 1.24


def length_to_grams(length_mm: float, density: float, diameter_mm: float = DEFAULT_DIAMETER_MM) -> float:
    radius_cm = (diameter_mm / 10.0) / 2.0
    volume_cm3 = math.pi * radius_cm * radius_cm * (length_mm / 10.0)
    return volume_cm3 * density


def _pick(rows: list, ams_attr: str = "ams_id", tray_attr: str = "tray_id"):
    for r in rows:
        if getattr(r, ams_attr) == EXTERNAL_AMS_ID and getattr(r, tray_attr) == EXTERNAL_TRAY_ID:
            return r
    return rows[0] if len(rows) == 1 else None


async def _spoolman_enabled(db: AsyncSession) -> tuple[bool, str]:
    rows = (
        (await db.execute(select(Settings).where(Settings.key.in_(("spoolman_enabled", "spoolman_url")))))
        .scalars()
        .all()
    )
    values = {r.key: r.value for r in rows}
    return (values.get("spoolman_enabled", "false").lower() == "true", (values.get("spoolman_url") or "").strip())


async def record_external_print_usage(
    db: AsyncSession,
    *,
    printer_id: int,
    filament_used_mm: float | None,
    print_name: str | None,
    status: str,
) -> dict | None:
    """Charge an external printer's job to its assigned spool.

    Returns ``{"spool_id", "grams", "mode"}`` when a spool was charged, else None.
    Never raises: accounting must not break print-complete handling.
    """
    try:
        if not isinstance(filament_used_mm, (int, float)) or filament_used_mm <= 0:
            return None
        enabled, url = await _spoolman_enabled(db)
        if enabled:
            return await _charge_spoolman(db, printer_id, float(filament_used_mm), url)
        return await _charge_internal(db, printer_id, float(filament_used_mm), print_name, status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("External usage accounting failed for printer %s: %s", printer_id, exc)
        return None


async def _charge_internal(
    db: AsyncSession, printer_id: int, length_mm: float, print_name: str | None, status: str
) -> dict | None:
    rows = list(
        (await db.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer_id))).scalars().all()
    )
    chosen = _pick(rows)
    if chosen is None:
        return None
    spool = await db.get(Spool, chosen.spool_id)
    if spool is None:
        return None

    grams = round(length_to_grams(length_mm, density_for(spool.material)), 2)
    spool.weight_used = float(spool.weight_used or 0) + grams
    spool.last_used = datetime.now(timezone.utc)
    label = float(spool.label_weight or 0)
    cost = round(grams / 1000.0 * spool.cost_per_kg, 4) if spool.cost_per_kg is not None else None
    db.add(
        SpoolUsageHistory(
            spool_id=spool.id,
            printer_id=printer_id,
            print_name=print_name,
            weight_used=grams,
            percent_used=int(round(grams / label * 100)) if label > 0 else 0,
            status=status,
            cost=cost,
        )
    )
    await db.commit()
    logger.info(
        "[USAGE] external printer %s: %.1f mm -> %.2f g charged to spool %s", printer_id, length_mm, grams, spool.id
    )
    return {"spool_id": spool.id, "grams": grams, "mode": "internal"}


async def _charge_spoolman(db: AsyncSession, printer_id: int, length_mm: float, url: str) -> dict | None:
    rows = list(
        (await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)))
        .scalars()
        .all()
    )
    chosen = _pick(rows)
    if chosen is None:
        return None

    from backend.app.services.spoolman import get_spoolman_client, init_spoolman_client

    client = await get_spoolman_client()
    if client is None or client.base_url != url.rstrip("/"):
        client = await init_spoolman_client(url)

    spool = await client.get_spool(chosen.spoolman_spool_id)
    filament = spool.get("filament") or {}
    density = filament.get("density") or density_for(filament.get("material"))
    diameter = filament.get("diameter") or DEFAULT_DIAMETER_MM
    grams = round(length_to_grams(length_mm, float(density), float(diameter)), 2)
    # Charged by weight rather than Spoolman's use_length so both inventory
    # modes go through one conversion, and the grams are known for the log.
    await client.use_spool(chosen.spoolman_spool_id, grams)
    logger.info(
        "[USAGE] external printer %s: %.1f mm -> %.2f g charged to Spoolman spool %s",
        printer_id,
        length_mm,
        grams,
        chosen.spoolman_spool_id,
    )
    return {"spool_id": chosen.spoolman_spool_id, "grams": grams, "mode": "spoolman"}
