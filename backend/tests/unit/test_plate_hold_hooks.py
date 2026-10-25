"""Hooks for a plate-aware client (Artisan Hub's reslicer): structured plate fields in
preflight, and swapping the file of a pending queue item."""

from __future__ import annotations

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer


class _FakeManager:
    def is_connected(self, pid):
        return True

    def get_status(self, pid):
        return None

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name, model="H2D", plate=None):
    p = Printer(
        name=name,
        serial_number=f"ph-{name}",
        ip_address="192.0.2.40",
        model=model,
        access_code="00000000",
        installed_plate=plate,
    )
    db.add(p)
    await db.flush()
    return p


async def _file(db, name, bed=None, sliced_for="H2D"):
    meta = {"sliced_for_model": sliced_for}
    if bed:
        meta["bed_type"] = bed
    f = LibraryFile(filename=name, file_path=f"/tmp/{name}", file_type="3mf", file_size=10, file_metadata=meta)
    db.add(f)
    await db.flush()
    return f


class TestPreflightPlateFields:
    async def test_plate_mismatch_is_structured(self, async_client, db_session, monkeypatch):
        cfx = await _printer(db_session, "H2D", plate="High Temp Plate")
        pei = await _printer(db_session, "H2D-b", plate="Textured PEI Plate")
        unknown = await _printer(db_session, "H2D-c", plate=None)
        f = await _file(db_session, "part.3mf", bed="Textured PEI Plate")
        item = PrintQueueItem(status="pending", position=1, library_file_id=f.id, target_model="H2D")
        db_session.add(item)
        await db_session.commit()
        from backend.app.services import printer_manager as pm_module
        from backend.app.services.print_scheduler import scheduler

        monkeypatch.setattr(pm_module, "printer_manager", _FakeManager())
        monkeypatch.setattr(scheduler, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
        r = await async_client.get(f"/api/v1/queue/{item.id}/preflight")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["job_plate"] == "Textured PEI Plate"
        assert "filament_types" in body
        rows = {row["printer_id"]: row for row in body["printers"]}
        assert rows[cfx.id]["plate_mismatch"] is True and rows[cfx.id]["has_plate"] == "High Temp Plate"
        assert rows[cfx.id]["eligible"] is False
        # same plate under another spelling is not a mismatch
        assert rows[pei.id]["plate_mismatch"] is False and rows[pei.id]["eligible"] is True
        # unknown installed plate: the check is skipped, so no mismatch
        assert rows[unknown.id]["plate_mismatch"] is False


class TestSwapFile:
    async def test_swap_file_and_pin(self, async_client, db_session):
        p = await _printer(db_session, "H2D", plate="High Temp Plate")
        old = await _file(db_session, "old.3mf", bed="Textured PEI Plate")
        new = await _file(db_session, "new.3mf", bed="High Temp Plate")
        item = PrintQueueItem(
            status="pending", position=1, library_file_id=old.id, target_model="H2D", priority=75, ams_mapping="[1,2]"
        )
        db_session.add(item)
        await db_session.commit()
        r = await async_client.patch(
            f"/api/v1/queue/{item.id}",
            json={"library_file_id": new.id, "printer_id": p.id, "target_model": None},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["library_file_id"] == new.id and body["printer_id"] == p.id
        assert body["priority"] == 75 and body["id"] == item.id
        await db_session.refresh(item)
        assert item.ams_mapping is None and item.archive_id is None

    async def test_rejects_wrong_model_and_bad_file(self, async_client, db_session):
        p = await _printer(db_session, "H2D", plate="High Temp Plate")
        old = await _file(db_session, "old.3mf")
        x1 = await _file(db_session, "x1.3mf", sliced_for="X1C")
        item = PrintQueueItem(status="pending", position=1, library_file_id=old.id, printer_id=p.id)
        db_session.add(item)
        await db_session.commit()
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"library_file_id": x1.id})
        assert r.status_code == 400 and "sliced for X1C" in r.text
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"library_file_id": 999999})
        assert r.status_code == 400
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"library_file_id": None})
        assert r.status_code == 400

    async def test_only_pending_items(self, async_client, db_session):
        p = await _printer(db_session, "H2D")
        f = await _file(db_session, "a.3mf")
        g = await _file(db_session, "b.3mf")
        item = PrintQueueItem(status="printing", position=1, library_file_id=f.id, printer_id=p.id)
        db_session.add(item)
        await db_session.commit()
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"library_file_id": g.id})
        assert r.status_code == 400


def test_supertack_spellings_are_one_plate():
    from backend.app.services.farm_match import normalize_plate

    assert normalize_plate("Cool Plate (SuperTack)") == normalize_plate("Supertack Plate") == "supertack"
    assert normalize_plate("Textured PEI Plate") == normalize_plate("textured_pei_plate") == "texturedpei"
    assert normalize_plate("Cool Plate") != normalize_plate("Supertack Plate")
