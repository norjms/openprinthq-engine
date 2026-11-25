"""Filament matching: colour difference, colour families, material groups, levels, the scheduler and the routes."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.app.models.filament_alias import FilamentAlias
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import filament_match as fm
from backend.app.services.print_scheduler import PrintScheduler

BLACK, JET, CHARCOAL, WHITE, RED = "#000000", "#101010", "#3E3E3E", "#FFFFFF", "#C12E1F"


def tray(gid, ftype, color, idx="", extruder=None):
    return {"global_tray_id": gid, "type": ftype, "color": color, "tray_info_idx": idx, "extruder_id": extruder}


def req(slot, ftype, color, idx="", nozzle=None):
    r = {"slot_id": slot, "type": ftype, "color": color, "tray_info_idx": idx, "used_grams": 12.5}
    if nozzle is not None:
        r["nozzle_id"] = nozzle
    return r


class TestColour:
    def test_delta_e(self):
        assert fm.delta_e(BLACK, BLACK) == 0
        assert fm.delta_e(BLACK, JET) < fm.CLOSE_DE
        assert fm.delta_e(BLACK, CHARCOAL) > fm.CLOSE_DE
        assert fm.delta_e(WHITE, RED) > 30

    @pytest.mark.parametrize(
        ("hexc", "fam"),
        [
            ("#000000", "black"),
            ("#3E3E3E", "grey"),
            ("#A6A9AA", "grey"),
            ("#FFFFFF", "white"),
            ("#E8DBB7", "white"),
            ("#C12E1F", "red"),
            ("#F5821F", "orange"),
            ("#F4EE2A", "yellow"),
            ("#00AE42", "green"),
            ("#0A2989", "blue"),
            ("#56B7E6", "blue"),
            ("#5E43B7", "purple"),
            ("#800080", "purple"),
            ("#F55A74", "pink"),
            ("#FFC0CB", "pink"),
            ("#7C4B00", "brown"),
        ],
    )
    def test_families(self, hexc, fam):
        assert fm.family(hexc) == fam

    def test_norm_hex(self):
        assert fm.norm_hex("000000FF") == "#000000"
        assert fm.norm_hex("#ab12cd") == "#AB12CD"
        assert fm.norm_hex("black") is None and fm.family(None) is None


class TestMaterial:
    def test_groups_and_finishes(self):
        assert fm.material("PLA Basic") == ("PLA", "")
        assert fm.material("pla+") == ("PLA", "")
        assert fm.material("PLA Matte") == ("PLA", "MATTE")
        assert fm.material("PETG HF") == ("PETG", "")
        assert fm.material("PLA-CF")[0] == "PLA-CF"
        assert fm.material("PAHT-CF")[0] == "PA-CF"
        assert fm.material("Weird", {"WEIRD": "PLA"}) == ("PLA", "")

    def test_compare_levels(self):
        near = fm.compare(req(1, "PLA", BLACK), tray(0, "PLA", JET), "close")
        assert near.ok and near.grade == "same" or near.grade == "close"
        assert not fm.compare(req(1, "PLA", BLACK), tray(0, "PLA", CHARCOAL), "close").ok
        assert not fm.compare(req(1, "PLA", BLACK), tray(0, "PLA", CHARCOAL), "family").ok  # grey is not black
        assert fm.compare(req(1, "PLA", "#0A2989"), tray(0, "PLA", "#56B7E6"), "family").ok  # two blues
        assert fm.compare(req(1, "PLA", BLACK), tray(0, "PLA", WHITE), "any").ok
        assert not fm.compare(req(1, "PETG", BLACK), tray(0, "PLA", BLACK), "any").ok
        assert not fm.compare(req(1, "PLA Silk", BLACK), tray(0, "PLA Matte", BLACK), "close").ok
        assert fm.compare(req(1, "PLA", BLACK), tray(0, "PLA Matte", BLACK), "close").ok  # finish on one side only
        exact = fm.compare(req(1, "PLA", BLACK, "GFA00"), tray(0, "PLA", "#050505", "GFA00"), "exact")
        assert exact.ok and exact.grade == "same"
        assert fm.compare(req(1, "PLA", None), tray(0, "PLA", WHITE), "exact").ok  # colour unknown never blocks

    def test_saved_mapping_counts_as_same(self):
        aliases = {fm.alias_key("PLA", BLACK, "PLA", CHARCOAL)}
        v = fm.compare(req(1, "PLA", BLACK), tray(0, "PLA", CHARCOAL), "exact", aliases=aliases)
        assert v.ok and v.note == "saved mapping"


class TestMatch:
    def test_best_tray_once_each(self):
        loaded = [tray(0, "PLA", CHARCOAL), tray(1, "PLA", JET), tray(2, "PLA", WHITE)]
        res = fm.match([req(1, "PLA", BLACK), req(2, "PLA", WHITE)], loaded, "close")
        assert res.ok and res.mapping == [1, 2]
        res = fm.match([req(1, "PLA", BLACK), req(2, "PLA", BLACK)], loaded, "close")
        assert not res.ok  # only one near-black spool
        assert res.slots[1].tray["global_tray_id"] == 0 and res.slots[1].grade == "different_colour"
        assert "filament 2 PLA #000000 (closest: PLA #3E3E3E" in res.summary()

    def test_nozzle_sides(self):
        loaded = [tray(0, "PLA", BLACK, extruder=1), tray(4, "PLA", BLACK, extruder=0)]
        res = fm.match([req(1, "PLA", BLACK, nozzle=0)], loaded)
        assert res.mapping == [4]
        assert fm.match([req(1, "PLA", BLACK, nozzle=0)], loaded[:1]).ok is False
        assert fm.match([req(1, "PLA", BLACK, nozzle=0)], loaded[:1], fts_installed=True).ok

    def test_check_mapping(self):
        loaded = [tray(0, "PLA", CHARCOAL), tray(1, "PLA", JET)]
        assert not fm.check_mapping([req(1, "PLA", BLACK)], loaded, [0]).ok
        assert fm.check_mapping([req(1, "PLA", BLACK)], loaded, [1]).ok
        assert fm.check_mapping([req(1, "PLA", BLACK)], loaded, [7]) is None
        assert fm.check_mapping([req(1, "PLA", BLACK)], loaded, None) is None

    def test_labels_and_reslice(self):
        assert fm.tray_label(tray(5, "PLA", BLACK)) == "AMS B slot 2"
        assert fm.tray_label(tray(128, "PLA", BLACK)) == "AMS HT 1"
        assert fm.tray_label({"global_tray_id": 254, "is_external": True}) == "External spool"
        assert fm.needs_reslice({"type": "PETG"}, {"type": "PLA"})
        assert not fm.needs_reslice({"type": "PLA Basic"}, {"type": "PLA"})


# ---------------------------------------------------------------- scheduler


def _ams(*trays):
    """A Bambu status with one AMS holding (type, color) per slot."""
    return SimpleNamespace(
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": i, "tray_type": t, "tray_color": c.lstrip("#") + "FF", "tray_info_idx": ""}
                        for i, (t, c) in enumerate(trays)
                    ],
                }
            ],
            "vt_tray": [],
        },
        nozzles=[],
        fila_switch=None,
    )


class _Mgr:
    def __init__(self, statuses):
        self.statuses = statuses

    def is_connected(self, pid):
        return True

    def get_status(self, pid):
        return self.statuses.get(pid)

    def is_awaiting_plate_clear(self, pid):
        return False


async def _printer(db, name):
    p = Printer(name=name, serial_number=f"fm-{name}", ip_address="192.0.2.30", model="X1C", access_code="0")
    db.add(p)
    await db.flush()
    return p


async def _item(db, **kw):
    it = PrintQueueItem(status="pending", position=1, **kw)
    db.add(it)
    await db.commit()
    return it


@pytest.fixture
def sched(monkeypatch):
    s = PrintScheduler()
    monkeypatch.setattr(s, "_is_printer_idle", lambda pid, require_plate_clear=True: True)
    return s


class TestScheduler:
    async def test_model_job_waits_and_says_why(self, db_session, monkeypatch, sched):
        a = await _printer(db_session, "A")
        b = await _printer(db_session, "B")
        await db_session.commit()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _Mgr({a.id: _ams(("PLA", CHARCOAL)), b.id: _ams(("PLA", JET))}),
        )
        fctx = await sched._filament_context(db_session)
        ctx = {**fctx, "requirements": [req(1, "PLA", BLACK)], "unmatched": []}
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", set(), filament_ctx=ctx)
        assert pid == b.id and reason is None
        ctx = {**fctx, "requirements": [req(1, "PLA", BLACK)], "unmatched": []}
        pid, reason = await sched._find_idle_printer_for_model(db_session, "X1C", {b.id}, filament_ctx=ctx)
        assert pid is None and "Waiting for filament: A (needs filament 1 PLA #000000" in reason
        assert [p.name for p, _ in ctx["unmatched"]] == ["A"]

    async def test_pinned_job_holds_until_mapped(self, db_session, monkeypatch, sched):
        a = await _printer(db_session, "A")
        await db_session.commit()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager", _Mgr({a.id: _ams(("PLA", CHARCOAL), ("PLA", JET))})
        )

        async def reqs(db, item):
            return [req(1, "PLA", BLACK), req(2, "PLA", WHITE)]

        monkeypatch.setattr(sched, "_get_filament_requirements", reqs)
        item = await _item(db_session, printer_id=a.id, ams_mapping=None)
        fctx = await sched._filament_context(db_session)
        assert await sched._hold_for_filament(db_session, item, a.id, fctx) is True
        rec = json.loads(item.filament_unmatched)
        assert rec["level"] == "close" and rec["printers"][0]["printer_name"] == "A"
        assert item.waiting_reason.startswith("Filament doesn't match: A: filament 2 PLA #FFFFFF")
        # Any colour lets it go, with the best mapping
        item.match_level = "any"
        assert await sched._hold_for_filament(db_session, item, a.id, fctx) is False
        assert item.filament_unmatched is None and json.loads(item.ams_mapping) == [1, 0]
        # A confirmed mapping is never second-guessed
        item.match_level = None
        item.filament_confirmed = True
        assert await sched._hold_for_filament(db_session, item, a.id, fctx) is False

    async def test_good_stored_mapping_is_kept_bad_one_replaced(self, db_session, monkeypatch, sched):
        a = await _printer(db_session, "A")
        await db_session.commit()
        monkeypatch.setattr(
            "backend.app.services.print_scheduler.printer_manager",
            _Mgr({a.id: _ams(("PLA", CHARCOAL), ("PLA", JET), ("PLA", "#050505"))}),
        )

        async def reqs(db, item):
            return [req(1, "PLA", BLACK)]

        monkeypatch.setattr(sched, "_get_filament_requirements", reqs)
        fctx = await sched._filament_context(db_session)
        item = await _item(db_session, printer_id=a.id, ams_mapping="[2]")
        assert await sched._hold_for_filament(db_session, item, a.id, fctx) is False
        assert item.ams_mapping == "[2]"  # already fits: kept
        item2 = await _item(db_session, printer_id=a.id, ams_mapping="[0]")
        assert await sched._hold_for_filament(db_session, item2, a.id, fctx) is False
        assert json.loads(item2.ams_mapping) in ([1], [2])  # charcoal replaced by a black

    async def test_saved_mapping_and_setting(self, db_session, monkeypatch, sched):
        from backend.app.models.settings import Settings

        db_session.add(FilamentAlias(req_type="PLA", req_color=BLACK, tray_type="PLA", tray_color=CHARCOAL))
        db_session.add(Settings(key="filament_match_level", value="exact"))
        await db_session.commit()
        fctx = await sched._filament_context(db_session)
        assert fctx["level"] == "exact" and fm.alias_key("PLA", BLACK, "PLA", CHARCOAL) in fctx["aliases"]


# ---------------------------------------------------------------- routes


class TestRoutes:
    async def test_settings_unmatched_options_mapping_aliases(self, async_client, db_session, monkeypatch):
        from backend.app.api.routes import filament_matching as route
        from backend.app.services.print_scheduler import scheduler

        a = await _printer(db_session, "H2D")
        await db_session.commit()
        mgr = _Mgr({a.id: _ams(("PLA", CHARCOAL), ("PETG", WHITE))})
        monkeypatch.setattr(route, "printer_manager", mgr)

        async def reqs(db, item):
            return [req(1, "PLA", BLACK), req(2, "PETG", WHITE)]

        monkeypatch.setattr(scheduler, "_get_filament_requirements", reqs)

        r = await async_client.get("/api/v1/filament-matching/settings")
        assert r.status_code == 200, r.text
        assert r.json()["level"] == "close" and "PLA BASIC" in r.json()["default_material_groups"]
        r = await async_client.put(
            "/api/v1/filament-matching/settings", json={"level": "family", "material_groups": {"pla pro max": "pla"}}
        )
        assert r.json()["level"] == "family" and r.json()["material_groups"] == {"PLA PRO MAX": "PLA"}
        assert (
            await async_client.put("/api/v1/filament-matching/settings", json={"level": "loose"})
        ).status_code == 422
        await async_client.put("/api/v1/filament-matching/settings", json={"level": "close"})

        unmatched = {
            "since": "2026-11-16T18:00:00+00:00",
            "level": "close",
            "printers": [{"printer_id": a.id, "printer_name": "H2D", "summary": "x", "slots": []}],
        }
        item = await _item(db_session, printer_id=a.id, filament_unmatched=json.dumps(unmatched))
        rows = (await async_client.get("/api/v1/filament-matching/unmatched")).json()
        assert [x["item_id"] for x in rows] == [item.id] and rows[0]["printers"][0]["printer_name"] == "H2D"

        opt = (await async_client.get(f"/api/v1/filament-matching/queue/{item.id}")).json()
        assert opt["level"] == "close" and len(opt["requirements"]) == 2
        p = opt["printers"][0]
        assert p["name"] == "H2D" and p["reports_filament"] and not p["match"]["ok"]
        assert [s["suggested"] for s in p["slots"]] == [0, 1]
        assert p["loaded"][1]["label"] == "AMS A slot 2"

        r = await async_client.post(
            f"/api/v1/filament-matching/queue/{item.id}/mapping",
            json={
                "printer_id": a.id,
                "slots": [{"slot_id": 1, "global_tray_id": 0}, {"slot_id": 2, "global_tray_id": 1}],
                "remember": True,
                "by": "james",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ams_mapping"] == [0, 1]
        await db_session.refresh(item)
        assert item.filament_confirmed and item.filament_unmatched is None and item.ams_mapping == "[0, 1]"
        assert (await async_client.get("/api/v1/filament-matching/unmatched")).json() == []

        aliases = (await async_client.get("/api/v1/filament-matching/aliases")).json()
        assert len(aliases) == 1  # only the slot that differed
        assert aliases[0]["requested"] == {"type": "PLA", "color": BLACK} and aliases[0]["loaded"]["color"] == CHARCOAL
        assert (await async_client.delete(f"/api/v1/filament-matching/aliases/{aliases[0]['id']}")).json() == {
            "removed": 1
        }
        assert (await async_client.delete(f"/api/v1/filament-matching/aliases/{aliases[0]['id']}")).status_code == 404

        item.status = "completed"
        await db_session.commit()
        r = await async_client.post(
            f"/api/v1/filament-matching/queue/{item.id}/mapping",
            json={"printer_id": a.id, "slots": [{"slot_id": 1, "global_tray_id": 0}]},
        )
        assert r.status_code == 400

    async def test_queue_item_level_and_confirmed(self, async_client, db_session):
        a = await _printer(db_session, "Q")
        await db_session.commit()
        item = await _item(db_session, printer_id=a.id)
        r = await async_client.patch(f"/api/v1/queue/{item.id}", json={"match_level": "exact"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (
            body["match_level"] == "exact"
            and body["filament_confirmed"] is False
            and body["filament_unmatched"] is None
        )
        assert (await async_client.patch(f"/api/v1/queue/{item.id}", json={"match_level": "loose"})).status_code == 422
