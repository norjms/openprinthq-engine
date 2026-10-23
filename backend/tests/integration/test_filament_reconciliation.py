"""Filament reconciliation: each scale weighing records expected against measured."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.filament_reconciliation import FilamentReconciliation
from backend.app.models.spool import Spool

API = "/api/v1/spoolbuddy"


@pytest.mark.asyncio
async def test_local_weighing_records_drift(async_client: AsyncClient, db_session):
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, weight_used=300)
    db_session.add(spool)
    await db_session.commit()
    # inventory expects 700 g left; the scale says 1150 gross -> 900 g net
    r = await async_client.post(f"{API}/scale/update-spool-weight", json={"spool_id": spool.id, "weight_grams": 1150})
    assert r.status_code == 200 and r.json()["weight_used"] == 100
    row = (await db_session.execute(select(FilamentReconciliation))).scalar_one()
    assert (row.backend, row.spool_id, row.material) == ("local", spool.id, "PLA")
    assert (row.expected_g, row.measured_g, row.drift_g) == (700.0, 900.0, 200.0)

    r = await async_client.get("/api/v1/inventory/reconciliation?flag_pct=10")
    body = r.json()
    assert r.status_code == 200 and body["weighings"] == 1
    assert body["by_material"]["PLA"]["total_drift_g"] == 200.0
    assert body["flagged_ids"] == [row.id]  # 200 g is more than 10% of 1000 g


@pytest.mark.asyncio
async def test_spoolman_weighing_records_drift(async_client: AsyncClient, db_session):
    client = AsyncMock()
    client.get_spool.return_value = {
        "remaining_weight": 400.0,
        "spool_weight": 200.0,
        "filament": {"weight": 1000.0, "material": "PETG"},
    }
    with patch("backend.app.api.routes.spoolbuddy._get_spoolman_client_or_none", AsyncMock(return_value=client)):
        r = await async_client.post(f"{API}/scale/update-spool-weight", json={"spool_id": 7, "weight_grams": 550})
    assert r.status_code == 200
    row = (await db_session.execute(select(FilamentReconciliation))).scalar_one()
    assert (row.backend, row.spool_id, row.material) == ("spoolman", 7, "PETG")
    assert (row.expected_g, row.measured_g, row.drift_g) == (400.0, 350.0, -50.0)
