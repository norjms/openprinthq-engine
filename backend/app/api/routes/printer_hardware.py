"""Printer hardware routes: nozzles, filament paths and what is loaded.

- ``GET    /printers/{id}/nozzles``
- ``PUT    /printers/{id}/nozzles/{extruder_index}``
- ``DELETE /printers/{id}/nozzles/{extruder_index}``
- ``GET    /printers/{id}/filament-paths``
- ``PUT    /printers/{id}/filament-paths``
- ``GET    /printers/{id}/loaded``

See ``services/printer_hardware.py`` for the vocabulary and numbering.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.models.user import User
from backend.app.services import printer_hardware as hw

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printers", tags=["printers"])

NozzleTypeLiteral = Literal["hardened_steel", "stainless_steel", "tungsten_carbide", "brass", "E3D", "undefine"]
VolumeTypeLiteral = Literal["Standard", "High Flow", "TPU High Flow", "E3D High Flow", "Hybrid", "Extra High Flow"]


class NozzleUpdate(BaseModel):
    diameter_mm: float = Field(..., gt=0, le=5)
    nozzle_type: NozzleTypeLiteral
    volume_type: VolumeTypeLiteral = "Standard"


class FilamentPathSlot(BaseModel):
    ams_id: int = Field(..., ge=0, le=255)
    tray_id: int | None = Field(default=None, ge=0, le=3)
    extruders: list[int] = Field(..., min_length=1, max_length=16)


class FilamentPathsUpdate(BaseModel):
    track_switch_mode: Literal["auto", "installed", "absent"] | None = None
    # None keeps the current manual rows; a list (even empty) replaces them.
    slots: list[FilamentPathSlot] | None = None


async def _get_printer(db: AsyncSession, printer_id: int) -> Printer:
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@router.get("/{printer_id}/nozzles")
async def get_printer_nozzles(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
) -> dict:
    """Nozzles per extruder with their merge source, plus H2C rack docks."""
    printer = await _get_printer(db, printer_id)
    return await hw.get_nozzles(db, printer)


@router.put("/{printer_id}/nozzles/{extruder_index}")
async def put_printer_nozzle(
    printer_id: int,
    body: NozzleUpdate,
    extruder_index: int = Path(..., ge=0, le=15),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
) -> dict:
    """Set a manual nozzle override for one extruder. Returns the merged nozzle view."""
    printer = await _get_printer(db, printer_id)
    await hw.set_manual_nozzle(db, printer.id, extruder_index, body.diameter_mm, body.nozzle_type, body.volume_type)
    await ws_manager.broadcast({"type": "printer_hardware_changed", "printer_id": printer.id})
    return await hw.get_nozzles(db, printer)


@router.delete("/{printer_id}/nozzles/{extruder_index}")
async def delete_printer_nozzle(
    printer_id: int,
    extruder_index: int = Path(..., ge=0, le=15),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
) -> dict:
    """Drop the manual override; live/stored MQTT values take over again."""
    printer = await _get_printer(db, printer_id)
    if not await hw.clear_manual_nozzle(db, printer.id, extruder_index):
        raise HTTPException(status_code=404, detail="No manual nozzle override for this extruder")
    await ws_manager.broadcast({"type": "printer_hardware_changed", "printer_id": printer.id})
    return await hw.get_nozzles(db, printer)


@router.get("/{printer_id}/filament-paths")
async def get_printer_filament_paths(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
) -> dict:
    """Which extruders each known slot can feed, and the track-switch state."""
    printer = await _get_printer(db, printer_id)
    return await hw.compute_filament_paths(db, printer)


@router.put("/{printer_id}/filament-paths")
async def put_printer_filament_paths(
    printer_id: int,
    body: FilamentPathsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
) -> dict:
    """Replace the manual filament-path rows and/or set the track-switch mode."""
    printer = await _get_printer(db, printer_id)
    slots = [s.model_dump() for s in body.slots] if body.slots is not None else None
    await hw.replace_manual_paths(db, printer, body.track_switch_mode, slots)
    await db.refresh(printer)
    await ws_manager.broadcast({"type": "printer_hardware_changed", "printer_id": printer.id})
    return await hw.compute_filament_paths(db, printer)


def _live_tray(state, ams_id: int, tray_id: int) -> dict | None:
    if state is None:
        return None
    raw = getattr(state, "raw_data", None) or {}
    tray: dict | None = None
    if ams_id == hw.EXTERNAL_AMS_ID:
        for vt in raw.get("vt_tray") or []:
            try:
                if isinstance(vt, dict) and int(vt.get("id", 254)) == 254 + tray_id:
                    tray = vt
                    break
            except (TypeError, ValueError):
                continue
    else:
        for unit in hw._ams_units_from_state(state):
            try:
                if int(unit.get("id", -1)) != ams_id:
                    continue
            except (TypeError, ValueError):
                continue
            for i, t in enumerate(unit.get("tray") or []):
                if not isinstance(t, dict):
                    continue
                try:
                    tid = int(t.get("id", i))
                except (TypeError, ValueError):
                    tid = i
                if tid == tray_id:
                    tray = t
                    break
    if tray is None:
        return None
    return {
        "tray_type": tray.get("tray_type") or None,
        "tray_color": tray.get("tray_color") or None,
        "tray_sub_brands": tray.get("tray_sub_brands") or None,
        "tray_info_idx": tray.get("tray_info_idx") or None,
        "remain": tray.get("remain"),
        "state": tray.get("state"),
    }


def _color_hex(rgba: str | None) -> str | None:
    if not rgba:
        return None
    return "#" + rgba.lstrip("#")[:6].upper()


@router.get("/{printer_id}/loaded")
async def get_printer_loaded(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
) -> dict:
    """What is installed: per slot the assigned spool, its extruder and live tray data."""
    from backend.app.api.routes.inventory import _ensure_spoolman_client, _load_settings_map, _spoolman_is_enabled
    from backend.app.services.printer_manager import printer_manager

    printer = await _get_printer(db, printer_id)
    paths = await hw.compute_filament_paths(db, printer)
    state = printer_manager.get_status(printer.id)
    settings = await _load_settings_map(db)
    spoolman_mode = _spoolman_is_enabled(settings)

    assignments: dict[tuple[int, int], dict] = {}
    if spoolman_mode:
        rows = (
            (await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer.id)))
            .scalars()
            .all()
        )
        client = await _ensure_spoolman_client(settings) if rows else None
        for row in rows:
            entry: dict = {
                "source": "spoolman",
                "spool_id": row.spoolman_spool_id,
                "extruder_index": row.extruder_index,
                "material": None,
                "color": None,
                "brand": None,
                "name": None,
                "remaining_g": None,
                "slicer_preset": None,
                "slicer_preset_name": None,
            }
            if client is not None:
                try:
                    from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool

                    mapped = _map_spoolman_spool(await client.get_spool(row.spoolman_spool_id))
                    entry.update(
                        material=mapped.get("material") or None,
                        color=_color_hex(mapped.get("rgba")),
                        brand=mapped.get("brand"),
                        name=mapped.get("color_name") or mapped.get("subtype"),
                        remaining_g=max(
                            0.0, float(mapped.get("label_weight") or 0) - float(mapped.get("weight_used") or 0)
                        ),
                        slicer_preset=mapped.get("slicer_filament") or None,
                        slicer_preset_name=mapped.get("slicer_filament_name") or None,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("loaded: Spoolman spool %s fetch failed: %s", row.spoolman_spool_id, exc)
            assignments[(row.ams_id, row.tray_id)] = entry
    else:
        rows = (
            (
                await db.execute(
                    select(SpoolAssignment)
                    .options(selectinload(SpoolAssignment.spool))
                    .where(SpoolAssignment.printer_id == printer.id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            spool: Spool | None = row.spool
            assignments[(row.ams_id, row.tray_id)] = {
                "source": "internal",
                "spool_id": row.spool_id,
                "extruder_index": row.extruder_index,
                "material": spool.material if spool else None,
                "color": _color_hex(spool.rgba) if spool else None,
                "brand": spool.brand if spool else None,
                "name": (spool.color_name or spool.subtype) if spool else None,
                "remaining_g": (
                    max(0.0, float(spool.label_weight or 0) - float(spool.weight_used or 0)) if spool else None
                ),
                "slicer_preset": spool.slicer_filament if spool else None,
                "slicer_preset_name": spool.slicer_filament_name if spool else None,
            }

    known = {(s["ams_id"], s["tray_id"]) for s in paths["slots"]}
    slots_out: list[dict] = []
    for slot in paths["slots"]:
        key = (slot["ams_id"], slot["tray_id"])
        assignment = assignments.get(key)
        extruder_index = assignment.get("extruder_index") if assignment else None
        if extruder_index is None and len(slot["extruders"]) == 1:
            extruder_index = slot["extruders"][0]
        slots_out.append(
            {
                **slot,
                "extruder_index": extruder_index,
                "assignment": assignment,
                "live": _live_tray(state, slot["ams_id"], slot["tray_id"]),
            }
        )
    # Assignments to slots the printer does not currently report (offline,
    # AMS unplugged) are still listed so nothing silently disappears.
    for key, assignment in assignments.items():
        if key in known:
            continue
        ams_id, tray_id = key
        slots_out.append(
            {
                "ams_id": ams_id,
                "tray_id": tray_id,
                "label": hw.slot_label(ams_id, tray_id, paths["nozzle_count"]),
                "ams_type": hw._ams_type(ams_id),
                "extruders": [],
                "fixed": False,
                "source": "default",
                "extruder_index": assignment.get("extruder_index"),
                "assignment": assignment,
                "live": None,
            }
        )

    return {
        "printer_id": printer.id,
        "printer_name": printer.name,
        "nozzle_count": paths["nozzle_count"],
        "track_switch": paths["track_switch"],
        "active_extruder": getattr(state, "active_extruder", None) if state is not None else None,
        "slots": slots_out,
    }
