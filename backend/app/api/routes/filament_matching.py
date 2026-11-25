"""Filament matching: unmatched jobs, the options to map one, saved mappings and settings (OpenPrintHQ farm feature).

See services/filament_match.py and docs/filament-matching.md.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import PrinterScope, get_printer_scope, is_printer_visible
from backend.app.models.filament_alias import FilamentAlias
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_group import PrinterGroup
from backend.app.models.settings import Settings
from backend.app.services import filament_match as fm
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.printer_models import is_gcode_compatible, normalize_printer_model

router = APIRouter(prefix="/filament-matching", tags=["filament-matching"])

Level = fm.LEVELS


class SettingsBody(BaseModel):
    level: str | None = Field(default=None, pattern="^(exact|close|family|any)$")
    material_groups: dict[str, str] | None = None


class SlotChoice(BaseModel):
    slot_id: int = Field(ge=1, le=64)
    global_tray_id: int = Field(ge=-1, le=255)


class MappingBody(BaseModel):
    printer_id: int
    slots: list[SlotChoice] = Field(min_length=1, max_length=64)
    remember: bool = True
    library_file_id: int | None = None
    by: str | None = Field(default=None, max_length=100)


async def _setting(db: AsyncSession, key: str) -> str | None:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _put_setting(db: AsyncSession, key: str, value: str) -> None:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if row is None:
        db.add(Settings(key=key, value=value))
    else:
        row.value = value


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


@router.get("/settings")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_READ_ALL),
):
    """The farm's default level and its material groups (the defaults merged with yours)."""
    level = await _setting(db, "filament_match_level") or fm.DEFAULT_LEVEL
    custom = _loads(await _setting(db, "filament_material_groups")) or {}
    return {
        "level": level if level in fm.LEVELS else fm.DEFAULT_LEVEL,
        "levels": list(fm.LEVELS),
        "material_groups": custom,
        "default_material_groups": fm.DEFAULT_GROUPS,
        "limits": {"exact": fm.EXACT_DE, "close": fm.CLOSE_DE},
    }


@router.put("/settings")
async def put_settings(
    body: SettingsBody,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    if body.level is not None:
        await _put_setting(db, "filament_match_level", body.level)
    if body.material_groups is not None:
        clean = {
            k.strip().upper(): v.strip().upper() for k, v in body.material_groups.items() if k.strip() and v.strip()
        }
        await _put_setting(db, "filament_material_groups", json.dumps(clean))
    await db.commit()
    return await get_settings(db)


async def _job_name(db: AsyncSession, item: PrintQueueItem) -> str:
    return await scheduler._get_job_name(db, item)  # noqa: SLF001


@router.get("/unmatched")
async def unmatched(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_READ_ALL),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """Pending jobs that can't start because of filament, oldest first, with why on each printer."""
    rows = (
        (
            await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.status == "pending", PrintQueueItem.filament_unmatched.is_not(None))
                .order_by(PrintQueueItem.priority.desc(), PrintQueueItem.position)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for item in rows:
        rec = _loads(item.filament_unmatched) or {}
        printers = [p for p in rec.get("printers", []) if await is_printer_visible(db, scope, p.get("printer_id"))]
        if not printers:
            continue
        out.append(
            {
                "item_id": item.id,
                "job_name": await _job_name(db, item),
                "printer_id": item.printer_id,
                "target_model": item.target_model,
                "target_group_id": item.target_group_id,
                "level": rec.get("level"),
                "since": rec.get("since"),
                "printers": printers,
            }
        )
    return out


async def _candidates(db: AsyncSession, item: PrintQueueItem) -> list[Printer]:
    if item.target_group_id:
        group = (
            await db.execute(
                select(PrinterGroup)
                .options(selectinload(PrinterGroup.printers))
                .where(PrinterGroup.id == item.target_group_id)
            )
        ).scalar_one_or_none()
        return [p for p in (group.printers if group else []) if p.is_active]
    if item.target_model:
        model = normalize_printer_model(item.target_model) or item.target_model
        q = select(Printer).where(func.lower(Printer.model) == model.lower(), Printer.is_active.is_(True))
        if item.target_location:
            q = q.where(Printer.location == item.target_location)
        return list((await db.execute(q)).scalars())
    if item.printer_id:
        return list((await db.execute(select(Printer).where(Printer.id == item.printer_id))).scalars())
    return []


async def _visible_item(db: AsyncSession, scope, item_id: int) -> PrintQueueItem:
    item = (
        await db.execute(
            select(PrintQueueItem)
            .options(selectinload(PrintQueueItem.archive), selectinload(PrintQueueItem.library_file))
            .where(PrintQueueItem.id == item_id)
        )
    ).scalar_one_or_none()
    if item is None or (item.printer_id and not await is_printer_visible(db, scope, item.printer_id)):
        raise HTTPException(404, "Queue item not found")
    return item


@router.get("/queue/{item_id}")
async def options(
    item_id: int,
    all_printers: bool = False,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_READ_ALL),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """What the job needs and, per printer it could go to, what is loaded and the best mapping at its level.

    With all_printers, every active printer is offered (a mapping onto another model needs a reslice)."""
    item = await _visible_item(db, scope, item_id)
    fctx = await scheduler._filament_context(db)  # noqa: SLF001
    reqs = await scheduler._get_filament_requirements(db, item) or []  # noqa: SLF001
    level = item.match_level if item.match_level in fm.LEVELS else fctx["level"]
    printers = await _candidates(db, item)
    if all_printers:
        seen = {p.id for p in printers}
        printers += [
            p
            for p in (await db.execute(select(Printer).where(Printer.is_active.is_(True)))).scalars()
            if p.id not in seen
        ]
    sliced_for = None
    if item.archive:
        sliced_for = item.archive.sliced_for_model
    elif item.library_file and item.library_file.file_metadata:
        sliced_for = item.library_file.file_metadata.get("sliced_for_model")
    out = []
    for p in printers:
        if not await is_printer_visible(db, scope, p.id):
            continue
        status = printer_manager.get_status(p.id)
        loaded = scheduler._build_loaded_filaments(status) if status is not None else []  # noqa: SLF001
        res = fm.match(reqs, loaded, level, fctx["groups"], fctx["aliases"]) if reqs and loaded else None
        same_model = sliced_for is None or is_gcode_compatible(sliced_for, p.model)
        out.append(
            {
                "printer_id": p.id,
                "name": p.name,
                "model": p.model,
                "connected": printer_manager.is_connected(p.id),
                "reports_filament": bool(loaded),
                "same_model": same_model,
                "loaded": [fm._tray_view(t) for t in loaded],  # noqa: SLF001
                "match": res.to_dict() if res else None,
                "slots": [
                    {
                        "slot_id": s.slot_id,
                        "suggested": s.tray["global_tray_id"] if s.tray else None,
                        "grade": s.grade,
                        "delta_e": s.delta_e,
                        "needs_reslice": bool(s.tray) and fm.needs_reslice({"type": s.type}, s.tray, fctx["groups"]),
                    }
                    for s in (res.slots if res else [])
                ],
            }
        )
    out.sort(key=lambda x: (not (x["match"] or {}).get("ok", False), not x["same_model"], not x["connected"]))
    return {
        "item_id": item.id,
        "job_name": await _job_name(db, item),
        "status": item.status,
        "level": level,
        "sliced_for_model": sliced_for,
        "requirements": reqs,
        "unmatched": _loads(item.filament_unmatched),
        "printers": out,
    }


@router.post("/queue/{item_id}/mapping")
async def set_mapping(
    item_id: int,
    body: MappingBody,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_UPDATE_ALL),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    """Print this job on a printer with the filament mapping someone chose (and, with remember, use it for the
    same filaments from now on). A new library_file_id is a resliced file for that printer."""
    item = await _visible_item(db, scope, item_id)
    if item.status != "pending":
        raise HTTPException(400, "Only a waiting job can be mapped")
    if item.dispatching_at is not None:
        raise HTTPException(409, "The job is being sent to a printer")
    printer = (await db.execute(select(Printer).where(Printer.id == body.printer_id))).scalar_one_or_none()
    if printer is None or not await is_printer_visible(db, scope, printer.id):
        raise HTTPException(404, "Printer not found")
    if body.library_file_id is not None:
        item.library_file_id = body.library_file_id
        item.archive_id = None
    mapping = [-1] * max(s.slot_id for s in body.slots)
    for s in body.slots:
        mapping[s.slot_id - 1] = s.global_tray_id
    if body.remember:
        reqs = {int(r.get("slot_id") or 0): r for r in (await scheduler._get_filament_requirements(db, item) or [])}  # noqa: SLF001
        status = printer_manager.get_status(printer.id)
        loaded = {t["global_tray_id"]: t for t in (scheduler._build_loaded_filaments(status) if status else [])}  # noqa: SLF001
        for s in body.slots:
            req, tray = reqs.get(s.slot_id), loaded.get(s.global_tray_id)
            if not req or not tray or fm.compare(req, tray, "exact").grade == "same":
                continue
            rk, tk = fm.alias_key(req.get("type"), req.get("color"), tray.get("type"), tray.get("color"))
            r_type, r_color = rk.split("|")
            t_type, t_color = tk.split("|")
            exists = (
                await db.execute(
                    select(FilamentAlias).where(
                        FilamentAlias.req_type == r_type,
                        FilamentAlias.req_color == r_color,
                        FilamentAlias.tray_type == t_type,
                        FilamentAlias.tray_color == t_color,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                db.add(
                    FilamentAlias(
                        req_type=r_type, req_color=r_color, tray_type=t_type, tray_color=t_color, created_by=body.by
                    )
                )
    item.printer_id = printer.id
    item.target_model = None
    item.target_group_id = None
    item.ams_mapping = json.dumps(mapping)
    item.filament_confirmed = True
    item.filament_unmatched = None
    item.waiting_reason = None
    await db.commit()
    return {"item_id": item.id, "printer_id": printer.id, "ams_mapping": mapping, "confirmed": True}


@router.get("/aliases")
async def list_aliases(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_READ_ALL),
):
    """Saved mappings: requested filament and the loaded filament accepted for it, newest first."""
    rows = (await db.execute(select(FilamentAlias).order_by(FilamentAlias.id.desc()))).scalars().all()
    return [
        {
            "id": r.id,
            "requested": {"type": r.req_type, "color": r.req_color},
            "loaded": {"type": r.tray_type, "color": r.tray_color},
            "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else r.created_at,
            "created_by": r.created_by,
        }
        for r in rows
    ]


@router.delete("/aliases/{alias_id}")
async def delete_alias(
    alias_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.QUEUE_UPDATE_ALL),
):
    row = await db.get(FilamentAlias, alias_id)
    if row is None:
        raise HTTPException(404, "Saved mapping not found")
    await db.delete(row)
    await db.commit()
    return {"removed": 1}
