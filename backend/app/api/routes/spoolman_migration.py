"""Routes for moving the built-in filament inventory into Spoolman.

    GET  /api/v1/spoolman/migration          current mode + last migration state
    POST /api/v1/spoolman/migration          {"spoolman_url": ..., "dry_run": false}

The POST works while Spoolman mode is still OFF (that is the point: it is what
turns it on), so it takes the URL in the body rather than reading settings.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.user import User
from backend.app.services import spoolman_migration as migration
from backend.app.services.spoolman import init_spoolman_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spoolman/migration", tags=["spoolman-migration"])


class MigrationRequest(BaseModel):
    spoolman_url: str = Field(..., min_length=1, max_length=512)
    dry_run: bool = False
    enable: bool = True


@router.get("")
async def get_migration_status(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
) -> dict:
    return await migration.migration_status(db)


@router.post("")
async def run_migration(
    body: MigrationRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
) -> dict:
    url = body.spoolman_url.strip().rstrip("/")
    try:
        assert_safe_spoolman_url(url)
        client = await init_spoolman_client(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    report = await migration.migrate_internal_to_spoolman(db, client, url, dry_run=body.dry_run, enable=body.enable)

    if report.mode_enabled:
        # Same follow-ups the settings switch runs.
        from backend.app.services.location_service import maybe_sync_spoolman_locations

        try:
            if await maybe_sync_spoolman_locations(db, client=client):
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spoolman migration: location sync failed: %s", exc)
        await ws_manager.broadcast({"type": "inventory_changed"})

    result = report.as_dict()
    if report.errors and not body.dry_run:
        raise HTTPException(status_code=502, detail=result)
    return result
