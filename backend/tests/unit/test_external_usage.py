"""Filament accounting for external (Klipper) printers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.services.external_usage import density_for, length_to_grams, record_external_print_usage


def test_length_to_grams_pla_one_metre():
    # 1 m of 1.75 mm PLA is about 2.98 g
    assert round(length_to_grams(1000.0, 1.24), 2) == 2.98


def test_density_longest_prefix_wins():
    assert density_for("PLA-CF") == 1.29
    assert density_for("pla silk") == 1.24
    assert density_for("PETG HF") == 1.27
    assert density_for(None) == 1.24


def test_status_map_captures_filament_used():
    from backend.app.services.klipper.status_map import apply_status_objects

    state = MagicMock()
    state.raw_data = {}
    apply_status_objects(state, {"print_stats": {"state": "complete", "filament_used": 1234.5}}, None)
    assert state.raw_data["filament_used_mm"] == 1234.5


@pytest.fixture
async def printer(db_session):
    from backend.app.models.printer import Printer

    p = Printer(name="Voron", serial_number="KLIPTEST01", ip_address="198.51.100.20", access_code="x")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.mark.asyncio
async def test_no_length_charges_nothing(db_session, printer):
    assert (
        await record_external_print_usage(
            db_session, printer_id=printer.id, filament_used_mm=0, print_name="x", status="completed"
        )
        is None
    )


@pytest.mark.asyncio
async def test_internal_mode_charges_assigned_external_slot(db_session, printer):
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    spool = Spool(material="PETG", label_weight=1000, weight_used=100, cost_per_kg=20.0)
    db_session.add(spool)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=255, tray_id=0))
    await db_session.commit()

    result = await record_external_print_usage(
        db_session, printer_id=printer.id, filament_used_mm=10000.0, print_name="part.gcode", status="cancelled"
    )
    assert result is not None and result["mode"] == "internal"
    expected = round(length_to_grams(10000.0, 1.27), 2)
    assert result["grams"] == expected
    await db_session.refresh(spool)
    assert spool.weight_used == pytest.approx(100 + expected)
    hist = (await db_session.execute(select(SpoolUsageHistory))).scalars().all()
    assert len(hist) == 1 and hist[0].status == "cancelled" and hist[0].cost == pytest.approx(expected / 1000 * 20)


@pytest.mark.asyncio
async def test_internal_mode_without_assignment_is_noop(db_session, printer):
    assert (
        await record_external_print_usage(
            db_session, printer_id=printer.id, filament_used_mm=500.0, print_name="x", status="completed"
        )
        is None
    )


@pytest.mark.asyncio
async def test_spoolman_mode_uses_filament_density(db_session, printer):
    from backend.app.models.settings import Settings
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://spoolman.test:8000"))
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=255, tray_id=0, spoolman_spool_id=42))
    await db_session.commit()

    client = MagicMock()
    client.base_url = "http://spoolman.test:8000"
    client.get_spool = AsyncMock(return_value={"id": 42, "filament": {"density": 1.04, "diameter": 1.75}})
    client.use_spool = AsyncMock(return_value={})
    with patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=client)):
        result = await record_external_print_usage(
            db_session, printer_id=printer.id, filament_used_mm=2000.0, print_name="x", status="completed"
        )
    expected = round(length_to_grams(2000.0, 1.04), 2)
    assert result == {"spool_id": 42, "grams": expected, "mode": "spoolman"}
    client.use_spool.assert_awaited_once_with(42, expected)
