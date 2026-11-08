"""API routes for printer groups.

Groups are a queue target: a job aimed at a group runs on whichever member
frees up first. Permissions reuse the existing ``printers:*`` family so no
role migration is needed for existing installs.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import PrinterScope, get_printer_scope, visible_printer_ids
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_group import PrinterGroup, group_name_key, printer_group_members
from backend.app.models.user import User
from backend.app.schemas.printer_group import (
    PrinterGroupCreate,
    PrinterGroupMember,
    PrinterGroupResponse,
    PrinterGroupUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printer-groups", tags=["printer-groups"])


def _to_response(group: PrinterGroup, visible: set[int] | None = None) -> PrinterGroupResponse:
    """``visible`` hides members outside a scoped caller's printer scope (#57)."""
    members = [
        PrinterGroupMember(
            id=p.id,
            name=p.name,
            model=p.model,
            location=p.location,
            connection_type=p.connection_type,
            is_active=p.is_active,
        )
        for p in sorted(group.printers, key=lambda p: p.name.lower())
        if visible is None or p.id in visible
    ]
    return PrinterGroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        color=group.color,
        position=group.position,
        printer_count=len(members),
        printers=members,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


async def _load(db: AsyncSession, group_id: int) -> PrinterGroup:
    result = await db.execute(select(PrinterGroup).where(PrinterGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Printer group not found")
    return group


async def _validate_printer_ids(db: AsyncSession, printer_ids: list[int], visible: set[int] | None = None) -> list[int]:
    """Return de-duplicated ids after confirming every one exists (and, for a
    scoped caller, is in its printer scope: a hidden printer is "unknown")."""
    unique = list(dict.fromkeys(printer_ids))
    if not unique:
        return []
    result = await db.execute(select(Printer.id).where(Printer.id.in_(unique)))
    found = set(result.scalars().all())
    if visible is not None:
        found &= visible
    missing = [pid for pid in unique if pid not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown printer id(s): {missing}")
    return unique


async def _set_members(db: AsyncSession, group_id: int, printer_ids: list[int]) -> None:
    await db.execute(delete(printer_group_members).where(printer_group_members.c.group_id == group_id))
    if printer_ids:
        await db.execute(
            insert(printer_group_members),
            [{"group_id": group_id, "printer_id": pid} for pid in printer_ids],
        )


async def _assert_name_free(db: AsyncSession, name: str, exclude_id: int | None = None) -> str:
    key = group_name_key(name)
    query = select(PrinterGroup.id).where(PrinterGroup.name_key == key)
    if exclude_id is not None:
        query = query.where(PrinterGroup.id != exclude_id)
    if (await db.execute(query)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"A printer group named '{name}' already exists")
    return key


@router.get("", response_model=list[PrinterGroupResponse])
@router.get("/", response_model=list[PrinterGroupResponse])
async def list_printer_groups(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    result = await db.execute(select(PrinterGroup).order_by(PrinterGroup.position, PrinterGroup.name))
    visible = await visible_printer_ids(db, scope)
    return [_to_response(g, visible) for g in result.scalars().all()]


@router.post("", response_model=PrinterGroupResponse, status_code=201)
@router.post("/", response_model=PrinterGroupResponse, status_code=201)
async def create_printer_group(
    data: PrinterGroupCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CREATE),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    key = await _assert_name_free(db, data.name)
    visible = await visible_printer_ids(db, scope)
    printer_ids = await _validate_printer_ids(db, data.printer_ids, visible)

    group = PrinterGroup(
        name=data.name,
        name_key=key,
        description=data.description,
        color=data.color,
        position=data.position,
    )
    db.add(group)
    await db.flush()
    await _set_members(db, group.id, printer_ids)
    await db.commit()

    group = await _load(db, group.id)
    await db.refresh(group, ["printers"])
    logger.info("Created printer group %s (%s) with %d printer(s)", group.id, group.name, len(printer_ids))
    return _to_response(group, visible)


@router.get("/{group_id}", response_model=PrinterGroupResponse)
async def get_printer_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    return _to_response(await _load(db, group_id), await visible_printer_ids(db, scope))


@router.put("/{group_id}", response_model=PrinterGroupResponse)
async def update_printer_group(
    group_id: int,
    data: PrinterGroupUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    group = await _load(db, group_id)

    if data.name is not None and group_name_key(data.name) != group.name_key:
        group.name_key = await _assert_name_free(db, data.name, exclude_id=group_id)
    if data.name is not None:
        group.name = data.name
    if data.description is not None:
        group.description = data.description
    if data.color is not None:
        group.color = data.color
    if data.position is not None:
        group.position = data.position

    visible = await visible_printer_ids(db, scope)
    if data.printer_ids is not None:
        printer_ids = await _validate_printer_ids(db, data.printer_ids, visible)
        if visible is not None:
            # A scoped caller cannot see, so must not silently drop, members
            # outside its scope: keep them.
            printer_ids += [p.id for p in group.printers if p.id not in visible and p.id not in printer_ids]
        await _set_members(db, group_id, printer_ids)

    await db.commit()
    group = await _load(db, group_id)
    await db.refresh(group, ["printers"])
    return _to_response(group, visible)


@router.post("/{group_id}/printers/{printer_id}", response_model=PrinterGroupResponse)
async def add_printer_to_group(
    group_id: int,
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    group = await _load(db, group_id)
    await _validate_printer_ids(db, [printer_id])
    if printer_id not in {p.id for p in group.printers}:
        await db.execute(insert(printer_group_members).values(group_id=group_id, printer_id=printer_id))
        await db.commit()
    group = await _load(db, group_id)
    await db.refresh(group, ["printers"])
    return _to_response(group, await visible_printer_ids(db, scope))


@router.delete("/{group_id}/printers/{printer_id}", response_model=PrinterGroupResponse)
async def remove_printer_from_group(
    group_id: int,
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    await _load(db, group_id)
    await db.execute(
        delete(printer_group_members).where(
            printer_group_members.c.group_id == group_id,
            printer_group_members.c.printer_id == printer_id,
        )
    )
    await db.commit()
    group = await _load(db, group_id)
    await db.refresh(group, ["printers"])
    return _to_response(group, await visible_printer_ids(db, scope))


@router.delete("/{group_id}", status_code=204)
async def delete_printer_group(
    group_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_DELETE),
    scope: PrinterScope | None = Depends(get_printer_scope),
):
    group = await _load(db, group_id)

    # Pending queue items aimed at this group would silently become
    # unschedulable, so refuse rather than orphan them. The FK is ON DELETE
    # SET NULL as a backstop for rows created concurrently.
    pending = await db.execute(
        select(PrintQueueItem.id).where(
            PrintQueueItem.target_group_id == group_id,
            PrintQueueItem.status == "pending",
        )
    )
    pending_ids = list(pending.scalars().all())
    if pending_ids:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(pending_ids)} pending queue item(s) target this group. "
                "Reassign or cancel them before deleting it."
            ),
        )

    await db.delete(group)
    await db.commit()
    logger.info("Deleted printer group %s (%s)", group_id, group.name)
    return None
