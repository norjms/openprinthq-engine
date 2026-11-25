"""Slicer-facing calls: the pre-send dry run (POST /queue/preflight) and the
duplicate lookup (GET /library/files/by-hash/{sha256})."""

from __future__ import annotations

import hashlib

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer


class _FakeManager:
    def __init__(self, offline=()):
        self.offline = set(offline)

    def is_connected(self, pid):
        return pid not in self.offline

    def get_status(self, pid):
        return None

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name, model="X1C", **kw):
    p = Printer(
        name=name, serial_number=f"sa-{name}", ip_address="192.0.2.40", model=model, access_code="00000000", **kw
    )
    db.add(p)
    await db.flush()
    return p


@pytest.fixture
def farm(monkeypatch):
    """Printers connected and idle; filament per printer set by the test."""
    from backend.app.services import printer_manager as pm_module
    from backend.app.services.print_scheduler import scheduler

    state = {"offline": set(), "missing": {}}
    monkeypatch.setattr(pm_module, "printer_manager", _FakeManager())
    monkeypatch.setattr(scheduler, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
    monkeypatch.setattr(
        scheduler,
        "_get_missing_filament_types",
        lambda pid, types: [t for t in types if t in state["missing"].get(pid, ())],
    )

    def apply():
        monkeypatch.setattr(pm_module, "printer_manager", _FakeManager(state["offline"]))

    state["apply"] = apply
    return state


class TestPreflightDryRun:
    async def test_every_printer_with_reasons(self, async_client, db_session, farm):
        a = await _printer(db_session, "A")
        b = await _printer(db_session, "B")
        await _printer(db_session, "C", out_of_service=True, out_of_service_reason="belt")
        d = await _printer(db_session, "D")
        await db_session.commit()
        farm["offline"] = {b.id}
        farm["missing"] = {d.id: {"PETG"}}
        farm["apply"]()
        r = await async_client.post("/api/v1/queue/preflight", json={"filament_types": ["petg", " pla "]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filament_types"] == ["PETG", "PLA"]
        by = {p["name"]: p for p in body["printers"]}
        assert by["A"]["eligible"] is True
        assert "offline" in by["B"]["reasons"]
        assert "out of service: belt" in by["C"]["reasons"]
        assert by["D"]["reasons"] == ["missing filament: PETG"]
        assert body["eligible"] == ["A"]
        assert a.id in {p["printer_id"] for p in body["printers"]}

    async def test_one_printer_and_explicit_needs(self, async_client, db_session, farm):
        p = await _printer(db_session, "Solo", model="H2D")
        await db_session.commit()
        r = await async_client.post(
            "/api/v1/queue/preflight",
            json={
                "printer_id": p.id,
                "nozzle_diameter": 0.6,
                "bed_type": "Textured PEI Plate",
                "sliced_for_model": "X1C",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["target"] == {"printer_id": p.id}
        assert body["needs"]["nozzle_diameter"] == 0.6 and body["job_plate"] == "Textured PEI Plate"
        [row] = body["printers"]
        assert row["name"] == "Solo" and "file sliced for X1C" in row["reasons"]

    async def test_by_model(self, async_client, db_session, farm):
        await _printer(db_session, "X1", model="X1C")
        await _printer(db_session, "H2", model="H2D")
        await db_session.commit()
        r = await async_client.post("/api/v1/queue/preflight", json={"target_model": "X1C"})
        assert [p["name"] for p in r.json()["printers"]] == ["X1"]

    async def test_uses_the_library_file_metadata(self, async_client, db_session, farm):
        await _printer(db_session, "M")
        f = LibraryFile(
            filename="part.gcode",
            file_path="/nonexistent/part.gcode",
            file_type="gcode",
            file_size=10,
            file_metadata={"nozzle_diameter": 0.4, "bed_type": "Cool Plate", "sliced_for_model": "X1C"},
        )
        db_session.add(f)
        await db_session.commit()
        r = await async_client.post("/api/v1/queue/preflight", json={"library_file_id": f.id})
        assert r.status_code == 200, r.text
        needs = r.json()["needs"]
        assert (
            needs["nozzle_diameter"] == 0.4 and needs["bed_type"] == "Cool Plate" and needs["sliced_for_model"] == "X1C"
        )
        # Explicit fields win over the file.
        r = await async_client.post("/api/v1/queue/preflight", json={"library_file_id": f.id, "nozzle_diameter": 0.6})
        assert r.json()["needs"]["nozzle_diameter"] == 0.6

    async def test_unknown_ids(self, async_client, db_session, farm):
        assert (await async_client.post("/api/v1/queue/preflight", json={"printer_id": 99999})).status_code == 404
        assert (await async_client.post("/api/v1/queue/preflight", json={"library_file_id": 99999})).status_code == 404

    async def test_nozzle_set_through_the_api_is_checked(self, async_client, db_session, farm):
        """A nozzle size set with PUT /printers/{id}/nozzles/{index} (a slicer's nozzle dropdown) is what the
        pre-send check compares against, also for printers that report no nozzles themselves."""
        p = await _printer(db_session, "MK4S", model="MK4S")
        await db_session.commit()
        r = await async_client.put(
            f"/api/v1/printers/{p.id}/nozzles/0", json={"diameter_mm": 0.6, "nozzle_type": "brass"}
        )
        assert r.status_code == 200, r.text
        r = await async_client.post("/api/v1/queue/preflight", json={"printer_id": p.id, "nozzle_diameter": 0.4})
        assert r.json()["printers"][0]["reasons"] == ["needs a 0.4mm nozzle (has 0.6mm)"]
        r = await async_client.post("/api/v1/queue/preflight", json={"printer_id": p.id, "nozzle_diameter": 0.6})
        assert r.json()["printers"][0]["eligible"] is True
        # Dropping the override goes back to what the printer reports (nothing here): no nozzle check.
        assert (await async_client.delete(f"/api/v1/printers/{p.id}/nozzles/0")).status_code == 200
        r = await async_client.post("/api/v1/queue/preflight", json={"printer_id": p.id, "nozzle_diameter": 0.4})
        assert r.json()["printers"][0]["eligible"] is True

    async def test_queued_item_preflight_sees_the_nozzle_override(self, async_client, db_session, farm):
        from backend.app.models.printer_hardware import PrinterNozzle

        p = await _printer(db_session, "Q2", model="MK4S")
        f = LibraryFile(
            filename="p.gcode",
            file_path="/nonexistent/p.gcode",
            file_type="gcode",
            file_size=10,
            file_metadata={"nozzle_diameter": 0.6},
        )
        db_session.add(f)
        await db_session.flush()
        # Written straight to the database, as another process would: the route reloads the overrides.
        db_session.add(PrinterNozzle(printer_id=p.id, extruder_index=0, diameter_mm=0.4, source="manual"))
        item = PrintQueueItem(status="pending", position=1, printer_id=p.id, library_file_id=f.id)
        db_session.add(item)
        await db_session.commit()
        r = await async_client.get(f"/api/v1/queue/{item.id}/preflight")
        assert r.status_code == 200, r.text
        assert r.json()["printers"][0]["reasons"] == ["needs a 0.6mm nozzle (has 0.4mm)"]

    async def test_queued_preflight_still_answers_the_same_way(self, async_client, db_session, farm):
        p = await _printer(db_session, "Q", out_of_service=True, out_of_service_reason="nozzle swap")
        item = PrintQueueItem(status="pending", position=1, printer_id=p.id)
        db_session.add(item)
        await db_session.commit()
        r = await async_client.get(f"/api/v1/queue/{item.id}/preflight")
        assert r.status_code == 200, r.text
        assert "out of service: nozzle swap" in r.json()["printers"][0]["reasons"]


class TestFileByHash:
    async def test_hit_miss_and_bad_input(self, async_client, db_session):
        h = hashlib.sha256(b"benchy").hexdigest()
        f = LibraryFile(filename="benchy.3mf", file_path="/x/benchy.3mf", file_type="3mf", file_size=6, file_hash=h)
        db_session.add(f)
        await db_session.commit()
        r = await async_client.get(f"/api/v1/library/files/by-hash/{h.upper()}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == f.id and r.json()["file_hash"] == h
        miss = hashlib.sha256(b"other").hexdigest()
        assert (await async_client.get(f"/api/v1/library/files/by-hash/{miss}")).status_code == 404
        assert (await async_client.get("/api/v1/library/files/by-hash/xyz")).status_code == 400

    async def test_trashed_files_do_not_count(self, async_client, db_session):
        from datetime import datetime

        h = hashlib.sha256(b"gone").hexdigest()
        db_session.add(
            LibraryFile(
                filename="gone.3mf",
                file_path="/x/gone.3mf",
                file_type="3mf",
                file_size=4,
                file_hash=h,
                deleted_at=datetime(2026, 1, 1),
            )
        )
        await db_session.commit()
        assert (await async_client.get(f"/api/v1/library/files/by-hash/{h}")).status_code == 404
