"""Integration tests for printer nozzles, filament paths, loaded slots,
extruder-aware slot assignments and printer-bound locations."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services import printer_hardware as hw
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_manager import printer_manager


def _dual_state(*, fila_switch: bool = False):
    """A dual-nozzle (H2D-like) state built through the real MQTT parser."""
    client = BambuMQTTClient(ip_address="192.0.2.20", serial_number="HWTEST01", access_code="12345678")
    device: dict = {
        "nozzle": {
            "info": [
                {"id": 0, "type": "HS01", "diameter": "0.4"},
                {"id": 1, "type": "SS00", "diameter": "0.6"},
            ]
        }
    }
    if fila_switch:
        device["fila_switch"] = {"in": [1, -1], "out": [0, 1], "stat": 0, "info": 0}
    client._process_message({"print": {"device": device}})
    client._process_message(
        {
            "print": {
                "ams": {
                    "ams": [
                        {
                            "id": 0,
                            "info": "10001003",
                            "tray": [{"id": i, "tray_type": "PLA", "tray_color": "FF0000FF"} for i in range(4)],
                        },
                        {"id": 1, "info": "10002104", "tray": [{"id": i} for i in range(4)]},
                    ],
                    "tray_now": "255",
                },
                "vt_tray": [{"id": 254, "tray_type": "PETG"}, {"id": 255, "tray_type": ""}],
            }
        }
    )
    return client.state


@pytest.fixture
async def spool_factory(db_session):
    from backend.app.models.spool import Spool

    async def _create_spool(**kwargs):
        defaults = {
            "material": "PLA",
            "brand": "Test Brand",
            "rgba": "FF0000FF",
            "label_weight": 1000,
            "weight_used": 0,
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create_spool


@pytest.fixture
async def dual_printer(printer_factory):
    hw._LAST_PERSISTED.clear()
    return await printer_factory(name="H2D Test", nozzle_count=2, model="H2D")


@pytest.fixture
async def single_printer(printer_factory):
    hw._LAST_PERSISTED.clear()
    return await printer_factory(name="P1S Test", nozzle_count=1, model="P1S")


class TestNozzles:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_without_state(self, async_client: AsyncClient, single_printer):
        with patch.object(printer_manager, "get_status", return_value=None):
            resp = await async_client.get(f"/api/v1/printers/{single_printer.id}/nozzles")
        assert resp.status_code == 200
        body = resp.json()
        assert body["nozzle_count"] == 1
        assert body["rack"] == []
        (n,) = body["nozzles"]
        assert n == {
            "extruder_index": 0,
            "position": "T0",
            "diameter_mm": 0.4,
            "nozzle_type": "undefine",
            "volume_type": "Standard",
            "source": "default",
            "live": None,
            "active": True,
        }

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_mqtt_values_and_manual_override(self, async_client: AsyncClient, dual_printer):
        state = _dual_state()
        with patch.object(printer_manager, "get_status", return_value=state):
            resp = await async_client.get(f"/api/v1/printers/{dual_printer.id}/nozzles")
            assert resp.status_code == 200
            body = resp.json()
            assert body["nozzle_count"] == 2
            right, left = body["nozzles"]
            assert right["position"] == "right" and left["position"] == "left"
            assert right["source"] == "mqtt"
            assert right["nozzle_type"] == "hardened_steel"
            assert right["volume_type"] == "High Flow"
            assert right["live"]["raw_type"] == "HS01"
            assert right["active"] is True and left["active"] is False
            assert left["diameter_mm"] == 0.6

            put = await async_client.put(
                f"/api/v1/printers/{dual_printer.id}/nozzles/1",
                json={"diameter_mm": 0.8, "nozzle_type": "tungsten_carbide", "volume_type": "High Flow"},
            )
            assert put.status_code == 200
            left = put.json()["nozzles"][1]
            assert left["source"] == "manual"
            assert left["diameter_mm"] == 0.8
            assert left["nozzle_type"] == "tungsten_carbide"
            # live block is still reported next to the override
            assert left["live"]["nozzle_type"] == "stainless_steel"

            delete = await async_client.delete(f"/api/v1/printers/{dual_printer.id}/nozzles/1")
            assert delete.status_code == 200
            left = delete.json()["nozzles"][1]
            assert left["source"] == "mqtt"
            assert left["diameter_mm"] == 0.6

            again = await async_client.delete(f"/api/v1/printers/{dual_printer.id}/nozzles/1")
            assert again.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_rejects_bad_enum(self, async_client: AsyncClient, single_printer):
        resp = await async_client.put(
            f"/api/v1/printers/{single_printer.id}/nozzles/0",
            json={"diameter_mm": 0.4, "nozzle_type": "copper", "volume_type": "Standard"},
        )
        assert resp.status_code == 422
        resp = await async_client.put(
            f"/api/v1/printers/{single_printer.id}/nozzles/0",
            json={"diameter_mm": 0.4, "nozzle_type": "brass", "volume_type": "Turbo"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_printer_404(self, async_client: AsyncClient):
        assert (await async_client.get("/api/v1/printers/99999/nozzles")).status_code == 404
        assert (await async_client.get("/api/v1/printers/99999/filament-paths")).status_code == 404
        assert (await async_client.get("/api/v1/printers/99999/loaded")).status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_observe_persists_mqtt_and_never_overwrites_manual(
        self, async_client: AsyncClient, dual_printer, db_session
    ):
        from sqlalchemy import select

        from backend.app.models.printer_hardware import PrinterNozzle

        pid = dual_printer.id
        state = _dual_state()
        assert await hw.observe_live_nozzles(pid, state) is True
        # unchanged values: no second write
        assert await hw.observe_live_nozzles(pid, state) is False

        rows = (await db_session.execute(select(PrinterNozzle).where(PrinterNozzle.printer_id == pid))).scalars().all()
        by_idx = {r.extruder_index: r for r in rows}
        assert by_idx[0].source == "mqtt" and by_idx[0].raw_type == "HS01"
        assert by_idx[1].nozzle_type == "stainless_steel"

        # Manual override then a changed live value: the manual row stays.
        with patch.object(printer_manager, "get_status", return_value=None):
            await async_client.put(
                f"/api/v1/printers/{pid}/nozzles/0",
                json={"diameter_mm": 0.2, "nozzle_type": "brass", "volume_type": "Standard"},
            )
        state.nozzles[0].nozzle_type = "SS00"
        await hw.observe_live_nozzles(pid, state)
        db_session.expire_all()
        row0 = (
            await db_session.execute(
                select(PrinterNozzle).where(PrinterNozzle.printer_id == pid, PrinterNozzle.extruder_index == 0)
            )
        ).scalar_one()
        assert row0.source == "manual" and row0.nozzle_type == "brass"

        # Offline afterwards: stored mqtt row is used for extruder 1.
        with patch.object(printer_manager, "get_status", return_value=None):
            body = (await async_client.get(f"/api/v1/printers/{pid}/nozzles")).json()
        assert body["nozzles"][1]["source"] == "mqtt"
        assert body["nozzles"][1]["diameter_mm"] == 0.6
        assert body["nozzles"][0]["source"] == "manual"


class TestFilamentPaths:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_paths_from_ams_extruder_map(self, async_client: AsyncClient, dual_printer):
        with patch.object(printer_manager, "get_status", return_value=_dual_state()):
            resp = await async_client.get(f"/api/v1/printers/{dual_printer.id}/filament-paths")
        assert resp.status_code == 200
        body = resp.json()
        assert body["track_switch"]["installed"] is False
        slots = {(s["ams_id"], s["tray_id"]): s for s in body["slots"]}
        assert slots[(0, 0)]["extruders"] == [0]
        assert slots[(0, 0)]["fixed"] is True
        assert slots[(0, 0)]["source"] == "mqtt"
        assert slots[(0, 0)]["label"] == "AMS A slot 1"
        assert slots[(1, 3)]["extruders"] == [1]
        assert slots[(255, 0)]["extruders"] == [1]
        assert slots[(255, 0)]["label"] == "External left"
        assert slots[(255, 1)]["extruders"] == [0]
        assert slots[(255, 1)]["label"] == "External right"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_track_switch_installed_feeds_both(self, async_client: AsyncClient, dual_printer):
        with patch.object(printer_manager, "get_status", return_value=_dual_state(fila_switch=True)):
            body = (await async_client.get(f"/api/v1/printers/{dual_printer.id}/filament-paths")).json()
            assert body["track_switch"]["installed"] is True
            assert body["track_switch"]["source"] == "mqtt"
            assert body["track_switch"]["inlets"] == {"A": 0, "B": None}
            slots = {(s["ams_id"], s["tray_id"]): s for s in body["slots"]}
            assert slots[(0, 2)]["extruders"] == [0, 1]
            assert slots[(0, 2)]["fixed"] is False
            # external holders are not routed through the switch
            assert slots[(255, 1)]["extruders"] == [0]

            # Forcing "absent" ignores the live switch.
            put = await async_client.put(
                f"/api/v1/printers/{dual_printer.id}/filament-paths", json={"track_switch_mode": "absent"}
            )
            assert put.status_code == 200
            body = put.json()
            assert body["track_switch"] == {
                "installed": False,
                "source": "manual",
                "mode": "absent",
                "inlets": {"A": 0, "B": None},
            }
            slots = {(s["ams_id"], s["tray_id"]): s for s in body["slots"]}
            assert slots[(0, 2)]["extruders"] == [0]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_manual_rows_override_and_replace(self, async_client: AsyncClient, single_printer):
        with patch.object(printer_manager, "get_status", return_value=None):
            resp = await async_client.put(
                f"/api/v1/printers/{single_printer.id}/filament-paths",
                json={
                    "track_switch_mode": "auto",
                    "slots": [
                        {"ams_id": 0, "tray_id": None, "extruders": [0, 1, 2]},
                        {"ams_id": 0, "tray_id": 3, "extruders": [2]},
                    ],
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            slots = {(s["ams_id"], s["tray_id"]): s for s in body["slots"]}
            assert slots[(0, 0)]["extruders"] == [0, 1, 2]
            assert slots[(0, 0)]["source"] == "manual"
            assert slots[(0, 3)]["extruders"] == [2]
            assert slots[(255, 0)]["extruders"] == [0]

            # Replacing with an empty list clears the manual rows.
            resp = await async_client.put(f"/api/v1/printers/{single_printer.id}/filament-paths", json={"slots": []})
            slots = {(s["ams_id"], s["tray_id"]) for s in resp.json()["slots"]}
            assert slots == {(255, 0)}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_rejects_bad_mode(self, async_client: AsyncClient, single_printer):
        resp = await async_client.put(
            f"/api/v1/printers/{single_printer.id}/filament-paths", json={"track_switch_mode": "maybe"}
        )
        assert resp.status_code == 422


class TestInternalAssignmentExtruder:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extruder_validation_and_inference(self, async_client: AsyncClient, dual_printer, spool_factory):
        spool = await spool_factory()
        state = _dual_state()
        with (
            patch.object(printer_manager, "get_status", return_value=state),
            patch.object(printer_manager, "get_client", return_value=None),
        ):
            bad = await async_client.post(
                "/api/v1/inventory/assignments",
                json={
                    "spool_id": spool.id,
                    "printer_id": dual_printer.id,
                    "ams_id": 0,
                    "tray_id": 1,
                    "extruder_index": 1,
                },
            )
            assert bad.status_code == 409
            assert bad.json()["allowed"] == [0]
            assert "detail" in bad.json()

            ok = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": dual_printer.id, "ams_id": 0, "tray_id": 1},
            )
            assert ok.status_code == 200
            assert ok.json()["extruder_index"] == 0

            listed = await async_client.get(f"/api/v1/inventory/assignments?printer_id={dual_printer.id}")
            assert listed.json()[0]["extruder_index"] == 0

            loaded = await async_client.get(f"/api/v1/printers/{dual_printer.id}/loaded")
            assert loaded.status_code == 200
            body = loaded.json()
            slot = next(s for s in body["slots"] if s["ams_id"] == 0 and s["tray_id"] == 1)
            assert slot["assignment"]["spool_id"] == spool.id
            assert slot["assignment"]["source"] == "internal"
            assert slot["extruder_index"] == 0
            assert slot["live"]["tray_type"] == "PLA"
            ext = next(s for s in body["slots"] if s["ams_id"] == 255 and s["tray_id"] == 0)
            assert ext["assignment"] is None
            assert ext["live"]["tray_type"] == "PETG"
            assert ext["extruder_index"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_track_switch_slot_accepts_either(self, async_client: AsyncClient, dual_printer, spool_factory):
        spool = await spool_factory()
        with (
            patch.object(printer_manager, "get_status", return_value=_dual_state(fila_switch=True)),
            patch.object(printer_manager, "get_client", return_value=None),
        ):
            none_given = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": dual_printer.id, "ams_id": 0, "tray_id": 2},
            )
            assert none_given.json()["extruder_index"] is None
            left = await async_client.post(
                "/api/v1/inventory/assignments",
                json={
                    "spool_id": spool.id,
                    "printer_id": dual_printer.id,
                    "ams_id": 0,
                    "tray_id": 2,
                    "extruder_index": 1,
                },
            )
            assert left.status_code == 200
            assert left.json()["extruder_index"] == 1


class TestLocations:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_new_fields_code_and_filters(self, async_client: AsyncClient):
        shelf = await async_client.post("/api/v1/inventory/locations", json={"name": "Rack 1"})
        assert shelf.status_code == 201
        shelf = shelf.json()
        assert shelf["kind"] == "storage"
        assert shelf["code"].startswith("L") and len(shelf["code"]) == 7

        box = await async_client.post(
            "/api/v1/inventory/locations",
            json={"name": "Dry box 1", "kind": "drybox", "parent_id": shelf["id"], "note": "silica"},
        )
        assert box.status_code == 201
        box = box.json()
        assert box["parent_id"] == shelf["id"] and box["note"] == "silica"

        dup_code = await async_client.post(
            "/api/v1/inventory/locations", json={"name": "Other", "code": shelf["code"].lower()}
        )
        assert dup_code.status_code == 409

        bad_kind = await async_client.post("/api/v1/inventory/locations", json={"name": "X", "kind": "garage"})
        assert bad_kind.status_code == 422

        only_dry = await async_client.get("/api/v1/inventory/locations?kind=drybox")
        assert [loc["id"] for loc in only_dry.json()] == [box["id"]]

        cycle = await async_client.patch(f"/api/v1/inventory/locations/{shelf['id']}", json={"parent_id": box["id"]})
        assert cycle.status_code == 422

        cleared = await async_client.patch(f"/api/v1/inventory/locations/{box['id']}", json={"parent_id": None})
        assert cleared.status_code == 200
        assert cleared.json()["parent_id"] is None
        assert cleared.json()["kind"] == "drybox"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_by_code_lists_internal_spools(self, async_client: AsyncClient):
        loc = (await async_client.post("/api/v1/inventory/locations", json={"name": "Bin 7"})).json()
        spool = (
            await async_client.post("/api/v1/inventory/spools", json={"material": "PLA", "location_id": loc["id"]})
        ).json()
        resp = await async_client.get(f"/api/v1/inventory/locations/by-code/{loc['code'].lower()}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == loc["id"]
        assert body["spools"] == [spool["id"]]
        assert body["spool_source"] == "internal"
        assert (await async_client.get("/api/v1/inventory/locations/by-code/LZZZZZZ")).status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sync_printers_idempotent_and_assignment_moves_spool(
        self, async_client: AsyncClient, dual_printer, spool_factory
    ):
        state = _dual_state()
        with (
            patch.object(printer_manager, "get_status", return_value=state),
            patch.object(printer_manager, "get_client", return_value=None),
        ):
            first = await async_client.post("/api/v1/inventory/locations/sync-printers")
            assert first.status_code == 200
            rows = first.json()
            kinds = sorted(r["kind"] for r in rows)
            # 1 printer, 2 AMS units, 8 slots, 2 external holders
            assert kinds.count("printer") == 1
            assert kinds.count("ams") == 2
            assert kinds.count("ams_slot") == 8
            assert kinds.count("external") == 2
            printer_loc = next(r for r in rows if r["kind"] == "printer")
            slot = next(r for r in rows if r["kind"] == "ams_slot" and r["ams_id"] == 0 and r["tray_id"] == 1)
            unit = next(r for r in rows if r["kind"] == "ams" and r["ams_id"] == 0)
            assert slot["name"] == "H2D Test - AMS A slot 2"
            assert slot["parent_id"] == unit["id"]
            assert unit["parent_id"] == printer_loc["id"]
            ext = next(r for r in rows if r["kind"] == "external" and r["tray_id"] == 0)
            assert ext["name"] == "H2D Test - External left"

            second = await async_client.post("/api/v1/inventory/locations/sync-printers")
            assert sorted(r["id"] for r in second.json()) == sorted(r["id"] for r in rows)

            by_printer = await async_client.get(f"/api/v1/inventory/locations?printer_id={dual_printer.id}")
            assert len(by_printer.json()) == len(rows)

            spool = await spool_factory()
            assigned = await async_client.post(
                "/api/v1/inventory/assignments",
                json={"spool_id": spool.id, "printer_id": dual_printer.id, "ams_id": 0, "tray_id": 1},
            )
            assert assigned.status_code == 200
            spool_after = (await async_client.get(f"/api/v1/inventory/spools/{spool.id}")).json()
            assert spool_after["location_id"] == slot["id"]

            scanned = (await async_client.get(f"/api/v1/inventory/locations/by-code/{slot['code']}")).json()
            assert scanned["spools"] == [spool.id]
            assert scanned["printer_name"] == "H2D Test"

            # Un-assigning leaves the location as it was.
            await async_client.delete(f"/api/v1/inventory/assignments/{dual_printer.id}/0/1")
            spool_after = (await async_client.get(f"/api/v1/inventory/spools/{spool.id}")).json()
            assert spool_after["location_id"] == slot["id"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_location_labels_pdf(self, async_client: AsyncClient):
        a = (await async_client.post("/api/v1/inventory/locations", json={"name": "Shelf Q"})).json()
        b = (await async_client.post("/api/v1/inventory/locations", json={"name": "Shelf R", "kind": "drybox"})).json()
        for template in ("avery5160", "62x29", "62x100"):
            resp = await async_client.post(
                "/api/v1/inventory/labels/locations",
                json={"location_ids": [a["id"], b["id"]], "template": template, "base_url": "https://hub.example"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"] == "application/pdf"
            assert resp.content.startswith(b"%PDF")
        missing = await async_client.post(
            "/api/v1/inventory/labels/locations", json={"location_ids": [999999], "template": "62x29"}
        )
        assert missing.status_code == 404
        bad = await async_client.post(
            "/api/v1/inventory/labels/locations", json={"location_ids": [a["id"]], "template": "a4"}
        )
        assert bad.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_location_label_qr_payload(self, async_client: AsyncClient):
        loc = (await async_client.post("/api/v1/inventory/locations", json={"name": "Shelf Z"})).json()
        captured = {}

        def _fake_render(template, data_list):
            captured["data"] = data_list
            return b"%PDF-1.4 fake"

        with patch("backend.app.api.routes.labels.render_location_labels", side_effect=_fake_render):
            resp = await async_client.post(
                "/api/v1/inventory/labels/locations",
                json={"location_ids": [loc["id"]], "template": "62x29", "base_url": "https://hub.example/"},
            )
        assert resp.status_code == 200
        assert captured["data"][0].qr_url == f"https://hub.example/l/{loc['code']}"
        assert captured["data"][0].kind == "storage"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_spool_label_hub_code_style(self, async_client: AsyncClient, spool_factory):
        spool = await spool_factory()
        captured = {}

        def _fake_render(template, data_list, monochrome=False):
            captured["data"] = data_list
            return b"%PDF-1.4 fake"

        with patch("backend.app.api.routes.labels.render_labels", side_effect=_fake_render):
            resp = await async_client.post(
                "/api/v1/inventory/labels",
                json={
                    "spool_ids": [spool.id],
                    "template": "box_62x29",
                    "code_style": "hub",
                    "base_url": "https://hub.example",
                },
            )
        assert resp.status_code == 200
        assert captured["data"][0].deeplink_url == f"https://hub.example/l/S{spool.id}"


SPOOLMAN_SPOOL = {
    "id": 42,
    "filament": {
        "id": 1,
        "name": "PLA Basic",
        "material": "PLA",
        "color_hex": "00FF00",
        "weight": 1000,
        "vendor": {"id": 1, "name": "Test Brand"},
    },
    "remaining_weight": 750.0,
    "used_weight": 250.0,
    "location": None,
    "registered": "2024-01-01T00:00:00+00:00",
    "archived": False,
    "extra": {},
}


@pytest.fixture
async def spoolman_settings(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


@pytest.fixture
def spoolman_client():
    client = MagicMock()
    client.base_url = "http://localhost:7912"
    client.get_spool = AsyncMock(return_value=SPOOLMAN_SPOOL)
    client.get_spools = AsyncMock(return_value=[])
    client.merge_spool_extra = AsyncMock(return_value={"id": 0, "extra": {}})
    client.update_spool = AsyncMock(return_value=SPOOLMAN_SPOOL)
    with (
        patch("backend.app.api.routes.spoolman_inventory._get_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.inventory._ensure_spoolman_client", AsyncMock(return_value=client)),
    ):
        yield client


class TestSpoolmanSlotExtruder:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slot_assignment_extruder_and_location(
        self, async_client: AsyncClient, dual_printer, spoolman_settings, spoolman_client
    ):
        state = _dual_state()
        with (
            patch.object(printer_manager, "get_status", return_value=state),
            patch.object(printer_manager, "get_client", return_value=None),
        ):
            bad = await async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={
                    "spoolman_spool_id": 42,
                    "printer_id": dual_printer.id,
                    "ams_id": 1,
                    "tray_id": 0,
                    "extruder_index": 0,
                },
            )
            assert bad.status_code == 409
            assert bad.json()["allowed"] == [1]

            await async_client.post("/api/v1/inventory/locations/sync-printers")

            ok = await async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={"spoolman_spool_id": 42, "printer_id": dual_printer.id, "ams_id": 1, "tray_id": 0},
            )
            assert ok.status_code == 200
            assert ok.json()["extruder_index"] == 1
            assert ok.json()["slot_location_id"] is not None
            spoolman_client.update_spool.assert_awaited_with(spool_id=42, location="H2D Test - AMS B slot 1")

            listed = await async_client.get("/api/v1/spoolman/inventory/slot-assignments/all")
            assert listed.json()[0]["extruder_index"] == 1

            loaded = (await async_client.get(f"/api/v1/printers/{dual_printer.id}/loaded")).json()
            slot = next(s for s in loaded["slots"] if s["ams_id"] == 1 and s["tray_id"] == 0)
            assert slot["assignment"]["source"] == "spoolman"
            assert slot["assignment"]["spool_id"] == 42
            assert slot["assignment"]["material"] == "PLA"
            assert slot["assignment"]["color"] == "#00FF00"
            assert slot["assignment"]["remaining_g"] == 750.0
            assert slot["extruder_index"] == 1
