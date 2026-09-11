"""Integration tests for POST/GET /api/v1/spoolman/migration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

URL = "http://spoolman.test:8000"


@pytest.fixture
async def printer(db_session):
    from backend.app.models.printer import Printer

    p = Printer(name="P", serial_number="MIGTEST001", ip_address="198.51.100.10", access_code="12345678")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.fixture
async def seeded(db_session, printer):
    from backend.app.models.location import Location
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_k_profile import SpoolKProfile

    loc = Location(name="Dry box A", name_key="dry box a")
    db_session.add(loc)
    await db_session.flush()

    rfid = Spool(
        material="PLA",
        subtype="Basic",
        brand="Bambu Lab",
        color_name="Jade White",
        rgba="FFFFFFFF",
        label_weight=1000,
        core_weight=250,
        weight_used=300,
        cost_per_kg=19.99,
        tray_uuid="A" * 32,
        tag_uid="0000000000000000",
        slicer_filament="GFA00",
        slicer_filament_name="Bambu PLA Basic",
        note="first",
    )
    generic = Spool(
        material="PETG",
        brand="Generic",
        rgba="000000FF",
        label_weight=1000,
        weight_used=1000,
        tag_uid="0000000000000000",
        location_id=loc.id,
    )
    db_session.add_all([rfid, generic])
    await db_session.flush()
    from datetime import datetime, timezone

    generic.archived_at = datetime.now(timezone.utc)
    db_session.add(SpoolKProfile(spool_id=rfid.id, printer_id=printer.id, k_value=0.02, name="K1"))
    db_session.add(SpoolAssignment(spool_id=rfid.id, printer_id=printer.id, ams_id=0, tray_id=2))
    await db_session.commit()
    return {"rfid": rfid.id, "generic": generic.id, "printer": printer.id}


def _client(existing: list[dict] | None = None):
    c = MagicMock()
    c.base_url = URL
    c.health_check = AsyncMock(return_value=True)
    c.ensure_extra_field = AsyncMock(return_value=True)
    c.get_all_spools = AsyncMock(return_value=existing or [])
    c.find_or_create_filament = AsyncMock(side_effect=[101, 102, 103, 104])
    ids = iter([501, 502, 503, 504])
    c.create_spool = AsyncMock(side_effect=lambda **kw: {"id": next(ids), **kw})
    c.update_spool_full = AsyncMock(return_value={})
    c.set_spool_archived = AsyncMock(return_value={})
    return c


@pytest.fixture
def no_location_sync():
    with patch(
        "backend.app.services.location_service.maybe_sync_spoolman_locations",
        AsyncMock(return_value=False),
    ):
        yield


class TestSpoolmanMigration:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_migrates_spools_profiles_assignments_and_switches_mode(
        self, async_client: AsyncClient, db_session, seeded, no_location_sync
    ):
        client = _client()
        with patch("backend.app.api.routes.spoolman_migration.init_spoolman_client", AsyncMock(return_value=client)):
            r = await async_client.post("/api/v1/spoolman/migration", json={"spoolman_url": URL})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["spools_created"] == 2
        assert body["k_profiles"] == 1
        assert body["assignments"] == 1
        assert body["mode_enabled"] is True

        calls = {kw["extra"]["bambuddy_spool_id"]: kw for _, kw in client.create_spool.call_args_list}
        rfid_call = calls[json.dumps(str(seeded["rfid"]))]
        assert rfid_call["remaining_weight"] == 700
        assert json.loads(rfid_call["extra"]["tag"]) == "A" * 32
        assert json.loads(rfid_call["extra"]["bambu_slicer_filament"]) == "GFA00"
        assert rfid_call["comment"] == "first"

        generic_call = calls[json.dumps(str(seeded["generic"]))]
        # all-zero tag_uid is a non-RFID spool and must not become a tag
        assert "tag" not in generic_call["extra"]
        assert generic_call["location"] == "Dry box A"
        assert generic_call["remaining_weight"] == 0
        client.set_spool_archived.assert_awaited_once()

        from backend.app.models.settings import Settings
        from backend.app.models.spool import Spool
        from backend.app.models.spool_assignment import SpoolAssignment
        from backend.app.models.spoolman_k_profile import SpoolmanKProfile
        from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

        db_session.expire_all()
        settings = {s.key: s.value for s in (await db_session.execute(select(Settings))).scalars().all()}
        assert settings["spoolman_enabled"] == "true"
        assert settings["spoolman_url"] == URL
        state = json.loads(settings["spoolman_migration"])
        assert state["status"] == "done"
        assert state["internal_assignments"][0]["tray_id"] == 2

        rfid_new = body["spool_map"][str(seeded["rfid"])]
        slot = (await db_session.execute(select(SpoolmanSlotAssignment))).scalars().all()
        assert [(s.ams_id, s.tray_id, s.spoolman_spool_id) for s in slot] == [(0, 2, rfid_new)]
        kps = (await db_session.execute(select(SpoolmanKProfile))).scalars().all()
        assert [(k.spoolman_spool_id, k.k_value) for k in kps] == [(rfid_new, 0.02)]
        assert (await db_session.execute(select(SpoolAssignment))).scalars().all() == []
        # built-in rows are kept
        assert len((await db_session.execute(select(Spool))).scalars().all()) == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rerun_skips_spools_already_in_spoolman(self, async_client: AsyncClient, seeded, no_location_sync):
        existing = [{"id": 900, "extra": {"bambuddy_spool_id": json.dumps(str(seeded["rfid"]))}}]
        client = _client(existing)
        with patch("backend.app.api.routes.spoolman_migration.init_spoolman_client", AsyncMock(return_value=client)):
            r = await async_client.post("/api/v1/spoolman/migration", json={"spoolman_url": URL})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["spools_existing"] == 1
        assert body["spools_created"] == 1
        assert body["spool_map"][str(seeded["rfid"])] == 900
        assert client.create_spool.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_dry_run_changes_nothing(self, async_client: AsyncClient, db_session, seeded):
        client = _client()
        with patch("backend.app.api.routes.spoolman_migration.init_spoolman_client", AsyncMock(return_value=client)):
            r = await async_client.post("/api/v1/spoolman/migration", json={"spoolman_url": URL, "dry_run": True})
        assert r.status_code == 200, r.text
        assert r.json()["spools_created"] == 2
        client.create_spool.assert_not_awaited()
        status = (await async_client.get("/api/v1/spoolman/migration")).json()
        assert status["spoolman_enabled"] is False
        assert status["migration"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unreachable_spoolman_leaves_mode_off(self, async_client: AsyncClient, seeded):
        client = _client()
        client.health_check = AsyncMock(return_value=False)
        with patch("backend.app.api.routes.spoolman_migration.init_spoolman_client", AsyncMock(return_value=client)):
            r = await async_client.post("/api/v1/spoolman/migration", json={"spoolman_url": URL})
        assert r.status_code == 502
        status = (await async_client.get("/api/v1/spoolman/migration")).json()
        assert status["spoolman_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_partial_failure_keeps_mode_off_and_records_state(
        self, async_client: AsyncClient, db_session, seeded
    ):
        client = _client()
        client.create_spool = AsyncMock(side_effect=[{"id": 501}, RuntimeError("boom")])
        with patch("backend.app.api.routes.spoolman_migration.init_spoolman_client", AsyncMock(return_value=client)):
            r = await async_client.post("/api/v1/spoolman/migration", json={"spoolman_url": URL})
        assert r.status_code == 502
        status = (await async_client.get("/api/v1/spoolman/migration")).json()
        assert status["spoolman_enabled"] is False
        assert status["migration"]["status"] == "partial"
