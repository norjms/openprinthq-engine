"""Move the built-in filament inventory into Spoolman and switch modes.

Bambuddy can keep its spool inventory in its own database or in Spoolman, and
flipping the setting has always been a one-way street for existing data: the
Spoolman side starts empty, and the switch deletes every built-in slot
assignment. This service does the move instead of the flip.

What it carries across, per spool:

- filament (material, subtype, brand, colour, net weight) via the same
  find-or-create path the Spoolman inventory UI uses, so no duplicate filaments
- remaining weight, empty-spool weight, price, location, note, archived state
- the RFID identity (tray_uuid preferred, else tag_uid) in ``extra.tag``, which
  is what AMS auto-sync matches on, so a tagged spool keeps its binding
- slicer preset and colour name in the ``extra`` keys Spoolman mode reads
- K-profiles, rewritten onto the new Spoolman spool id
- AMS slot assignments, rewritten into ``spoolman_slot_assignments``

It is idempotent. Every created spool carries ``extra.bambuddy_spool_id``; a
re-run after a partial failure finds those and only creates what is missing.

Built-in spool rows are NOT deleted. The built-in slot assignments are, exactly
as the settings switch does, because the missing-assignment check unions both
tables; a snapshot of them is kept in the ``spoolman_migration`` setting so the
move can be reversed by hand.

Fields Spoolman has no home for are not migrated: weight lock, last scale
reading, nozzle max temperature, category, low-stock threshold and per-print
usage history (which stays in the local database under the old spool ids).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_k_profile import SpoolmanKProfile
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services.spoolman import SpoolmanClient

logger = logging.getLogger(__name__)

MIGRATION_SETTING = "spoolman_migration"
ORIGIN_FIELD = "bambuddy_spool_id"
_HEX = set("0123456789ABCDEF")


@dataclass
class MigrationReport:
    dry_run: bool
    spools_total: int = 0
    spools_created: int = 0
    spools_existing: int = 0
    k_profiles: int = 0
    assignments: int = 0
    mode_enabled: bool = False
    spool_map: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "spools_total": self.spools_total,
            "spools_created": self.spools_created,
            "spools_existing": self.spools_existing,
            "k_profiles": self.k_profiles,
            "assignments": self.assignments,
            "mode_enabled": self.mode_enabled,
            "spool_map": {str(k): v for k, v in self.spool_map.items()},
            "errors": self.errors,
        }


def _hex_tag(value: str | None, length: int | None = None) -> str | None:
    tag = (value or "").strip().upper()
    if not tag or any(c not in _HEX for c in tag):
        return None
    if length is not None and len(tag) != length:
        return None
    return tag


def spool_tag(spool: Spool) -> str | None:
    """The identity AMS sync matches on: the Bambu tray UUID, else the RFID UID.

    All-zero values are what the AMS reports for a non-RFID spool and must not
    become a tag, or every generic spool would claim the same identity.
    """
    for candidate in (_hex_tag(spool.tray_uuid, 32), _hex_tag(spool.tag_uid)):
        if candidate and candidate.strip("0"):
            return candidate
    return None


def _extract_str(extra: dict, key: str) -> str | None:
    raw = extra.get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = raw
    return str(value) if value not in (None, "") else None


async def _get_setting(db: AsyncSession, key: str) -> str | None:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(db: AsyncSession, key: str, value: str) -> None:
    from backend.app.core.db_dialect import upsert_setting

    await upsert_setting(db, Settings, key, value)


async def migration_status(db: AsyncSession) -> dict:
    raw = await _get_setting(db, MIGRATION_SETTING)
    state: dict = {}
    if raw:
        try:
            state = json.loads(raw)
        except ValueError:
            state = {"status": "unreadable"}
    return {
        "spoolman_enabled": (await _get_setting(db, "spoolman_enabled") or "false").lower() == "true",
        "spoolman_url": await _get_setting(db, "spoolman_url") or "",
        "internal_spools": len((await db.execute(select(Spool.id))).all()),
        "migration": state or None,
    }


async def _existing_origin_map(client: SpoolmanClient) -> dict[int, int]:
    """Map built-in spool id -> Spoolman spool id for spools a previous run created."""
    found: dict[int, int] = {}
    for s in await client.get_all_spools(allow_archived=True):
        origin = _extract_str(s.get("extra") or {}, ORIGIN_FIELD)
        if origin and origin.isdigit() and s.get("id"):
            found[int(origin)] = int(s["id"])
    return found


async def _create_one(client: SpoolmanClient, spool: Spool, location: str | None) -> int:
    color_hex = (spool.rgba or "808080FF")[:6]
    filament_id = await client.find_or_create_filament(
        material=spool.material,
        subtype=spool.subtype or "",
        brand=spool.brand,
        color_hex=color_hex,
        label_weight=int(spool.label_weight or 1000),
        color_name=spool.color_name,
    )
    remaining = max(0.0, float(spool.label_weight or 0) - float(spool.weight_used or 0))
    extra: dict[str, str] = {ORIGIN_FIELD: json.dumps(str(spool.id))}
    tag = spool_tag(spool)
    if tag:
        extra["tag"] = json.dumps(tag)
    if spool.slicer_filament:
        extra["bambu_slicer_filament"] = json.dumps(spool.slicer_filament)
    if spool.slicer_filament_name:
        extra["bambu_slicer_filament_name"] = json.dumps(spool.slicer_filament_name)
    if spool.color_name:
        extra["bambu_color_name"] = json.dumps(spool.color_name)

    created = await client.create_spool(
        filament_id=filament_id,
        remaining_weight=remaining,
        location=location or None,
        comment=spool.note or None,
        extra=extra,
    )
    new_id = int(created["id"])

    # Price and empty-spool weight are not accepted on create by the client, so
    # they go on as a second write. Both are best-effort: the spool exists and is
    # correctly bound either way.
    if spool.cost_per_kg is not None or spool.core_weight is not None:
        try:
            await client.update_spool_full(
                new_id,
                price=spool.cost_per_kg,
                spool_weight=float(spool.core_weight) if spool.core_weight is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Spoolman migration: price/spool_weight not set on spool %s: %s", new_id, exc)

    if spool.archived_at is not None:
        await client.set_spool_archived(new_id, archived=True)
    return new_id


async def migrate_internal_to_spoolman(
    db: AsyncSession,
    client: SpoolmanClient,
    spoolman_url: str,
    *,
    dry_run: bool = False,
    enable: bool = True,
) -> MigrationReport:
    """Copy every built-in spool into Spoolman, rebind slots, then switch modes."""
    report = MigrationReport(dry_run=dry_run)

    spools = list(
        (
            await db.execute(
                select(Spool).options(selectinload(Spool.k_profiles), selectinload(Spool.location)).order_by(Spool.id)
            )
        )
        .scalars()
        .all()
    )
    assignments = list((await db.execute(select(SpoolAssignment))).scalars().all())
    report.spools_total = len(spools)

    if not await client.health_check():
        report.errors.append(f"Spoolman is not reachable at {spoolman_url}")
        return report

    for name in ("tag", ORIGIN_FIELD, "bambu_slicer_filament", "bambu_slicer_filament_name", "bambu_color_name"):
        if not dry_run and not await client.ensure_extra_field(name):
            report.errors.append(f"could not register Spoolman extra field {name}")
    if report.errors:
        return report

    existing = await _existing_origin_map(client)

    for spool in spools:
        if spool.id in existing:
            report.spool_map[spool.id] = existing[spool.id]
            report.spools_existing += 1
            continue
        if dry_run:
            continue
        location = spool.location.name if spool.location is not None else spool.storage_location
        try:
            report.spool_map[spool.id] = await _create_one(client, spool, location)
            report.spools_created += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"spool {spool.id}: {type(exc).__name__}: {exc}")

    if dry_run:
        report.spools_created = report.spools_total - report.spools_existing
        report.k_profiles = sum(len(s.k_profiles) for s in spools)
        report.assignments = len(assignments)
        return report

    if report.errors:
        # Leave the mode alone. A re-run picks up from here via bambuddy_spool_id.
        await _write_state(db, "partial", spoolman_url, report, assignments)
        await db.commit()
        return report

    # K-profiles: replace whatever is stored for the new ids, so a re-run cannot
    # double them.
    new_ids = list(report.spool_map.values())
    if new_ids:
        await db.execute(delete(SpoolmanKProfile).where(SpoolmanKProfile.spoolman_spool_id.in_(new_ids)))
    for spool in spools:
        for kp in spool.k_profiles:
            db.add(
                SpoolmanKProfile(
                    spoolman_spool_id=report.spool_map[spool.id],
                    printer_id=kp.printer_id,
                    extruder=kp.extruder,
                    nozzle_diameter=kp.nozzle_diameter,
                    nozzle_type=kp.nozzle_type,
                    k_value=kp.k_value,
                    name=kp.name,
                    cali_idx=kp.cali_idx,
                    setting_id=kp.setting_id,
                )
            )
            report.k_profiles += 1

    # Slot bindings. The table is unique on (printer_id, ams_id, tray_id).
    await db.execute(delete(SpoolmanSlotAssignment))
    for a in assignments:
        target = report.spool_map.get(a.spool_id)
        if target is None:
            continue
        db.add(
            SpoolmanSlotAssignment(
                printer_id=a.printer_id,
                ams_id=a.ams_id,
                tray_id=a.tray_id,
                spoolman_spool_id=target,
            )
        )
        report.assignments += 1

    if enable:
        await _set_setting(db, "spoolman_url", spoolman_url)
        await _set_setting(db, "spoolman_enabled", "true")
        await db.execute(delete(SpoolAssignment))
        report.mode_enabled = True

    await _write_state(db, "done", spoolman_url, report, assignments)
    await db.commit()
    return report


async def _write_state(
    db: AsyncSession,
    status: str,
    url: str,
    report: MigrationReport,
    assignments: list[SpoolAssignment],
) -> None:
    state = {
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
        "spoolman_url": url,
        "report": report.as_dict(),
        # Enough to put the built-in slot bindings back by hand if the move is
        # ever reversed: the switch itself deletes them.
        "internal_assignments": [
            {"spool_id": a.spool_id, "printer_id": a.printer_id, "ams_id": a.ams_id, "tray_id": a.tray_id}
            for a in assignments
        ],
    }
    await _set_setting(db, MIGRATION_SETTING, json.dumps(state))
