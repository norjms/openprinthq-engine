"""Bed-ejection profiles and manual ejects (OpenPrintHQ farm feature).

See services/bed_ejection.py for what an eject does and the guards around it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.ejection_profile import EjectionProfile
from backend.app.models.printer import Printer
from backend.app.services import bed_ejection
from backend.app.services.printer_manager import printer_manager

router = APIRouter(tags=["ejection"])


class EjectionProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    gcode: str = Field(..., min_length=1, max_length=20000)
    max_bed_temp_c: float | None = Field(default=None, ge=0, le=150)
    timeout_s: int = Field(default=1800, ge=30, le=14400)
    settle_s: int = Field(default=30, ge=0, le=3600)
    max_consecutive: int = Field(default=5, ge=0, le=1000)
    allowed_plates: list[str] | None = None
    verify_with_camera: bool = False


class EjectionProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    gcode: str | None = Field(default=None, min_length=1, max_length=20000)
    max_bed_temp_c: float | None = Field(default=None, ge=0, le=150)
    timeout_s: int | None = Field(default=None, ge=30, le=14400)
    settle_s: int | None = Field(default=None, ge=0, le=3600)
    max_consecutive: int | None = Field(default=None, ge=0, le=1000)
    allowed_plates: list[str] | None = None
    verify_with_camera: bool | None = None


class EjectionProfileOut(EjectionProfileIn):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class EjectStatus(BaseModel):
    printer_id: int
    auto_eject: bool
    ejection_profile_id: int | None
    consecutive_ejects: int
    ejecting: bool
    last: dict | None = None


@router.get("/ejection-profiles", response_model=list[EjectionProfileOut])
async def list_profiles(
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    rows = (await db.execute(select(EjectionProfile).order_by(EjectionProfile.name))).scalars().all()
    return rows


@router.post("/ejection-profiles", response_model=EjectionProfileOut, status_code=201)
async def create_profile(
    body: EjectionProfileIn,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
):
    p = EjectionProfile(**body.model_dump())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@router.patch("/ejection-profiles/{profile_id}", response_model=EjectionProfileOut)
async def update_profile(
    profile_id: int,
    body: EjectionProfilePatch,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
):
    p = await db.get(EjectionProfile, profile_id)
    if p is None:
        raise HTTPException(404, "Ejection profile not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    await db.commit()
    await db.refresh(p)
    return p


@router.delete("/ejection-profiles/{profile_id}")
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
):
    p = await db.get(EjectionProfile, profile_id)
    if p is None:
        raise HTTPException(404, "Ejection profile not found")
    # Printers using it fall back to no profile (and therefore no auto-eject).
    await db.execute(update(Printer).where(Printer.ejection_profile_id == profile_id).values(ejection_profile_id=None))
    await db.delete(p)
    await db.commit()
    return {"success": True}


async def _status(db: AsyncSession, printer_id: int) -> EjectStatus:
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(404, "Printer not found")
    last = bed_ejection.LAST_RESULT.get(printer_id)
    return EjectStatus(
        printer_id=printer_id,
        auto_eject=bool(printer.auto_eject),
        ejection_profile_id=printer.ejection_profile_id,
        consecutive_ejects=printer.consecutive_ejects or 0,
        ejecting=bed_ejection.is_ejecting(printer_id),
        last=asdict(last) if last else None,
    )


@router.get("/printers/{printer_id}/eject", response_model=EjectStatus)
async def eject_status(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    return await _status(db, printer_id)


@router.post("/printers/{printer_id}/eject", response_model=EjectStatus, status_code=202)
async def eject_now(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    """Run the printer's ejection profile now (a person asked for it).

    The plate and consecutive-eject guards don't apply (a person is deciding),
    but the printer must be idle and connected, and the bed-cool wait, idle check
    and optional camera check still run before the gate is cleared.
    """
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(404, "Printer not found")
    if not printer.ejection_profile_id:
        raise HTTPException(400, "No ejection profile set for this printer")
    if bed_ejection.is_ejecting(printer_id):
        raise HTTPException(409, "An eject is already running")
    if not printer_manager.is_connected(printer_id):
        raise HTTPException(400, "Printer not connected")
    st = printer_manager.get_status(printer_id)
    if not st or st.state not in bed_ejection.IDLE_STATES:
        raise HTTPException(409, f"Printer is not idle (state={st.state if st else 'unknown'})")
    bed_ejection.start(printer_id, printer.ejection_profile_id)
    return await _status(db, printer_id)
