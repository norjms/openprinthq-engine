"""Tests for printer groups and group-targeted queue items.

Covers the three layers the feature touches:

* the model / membership table (many-to-many, a printer may be in several groups)
* the ``/printer-groups`` routes (CRUD, membership edits, delete guard)
* the scheduler's group branch, which must behave exactly like the existing
  model-based branch since both now share ``_select_idle_printer``
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_group import PrinterGroup, group_name_key, printer_group_members
from backend.app.services.print_scheduler import PrintScheduler


async def _mk_printer(db, name: str, model: str = "H2C", active: bool = True) -> Printer:
    printer = Printer(
        name=name,
        serial_number=f"test-{name}",
        ip_address="192.0.2.10",
        model=model,
        access_code="00000000",
        is_active=active,
    )
    db.add(printer)
    await db.flush()
    return printer


async def _mk_group(db, name: str, printer_ids: list[int] | None = None) -> PrinterGroup:
    group = PrinterGroup(name=name, name_key=group_name_key(name))
    db.add(group)
    await db.flush()
    for pid in printer_ids or []:
        await db.execute(insert(printer_group_members).values(group_id=group.id, printer_id=pid))
    await db.commit()
    return group


class TestPrinterGroupModel:
    async def test_membership_round_trips(self, db_session):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        group = await _mk_group(db_session, "Farm", [a.id, b.id])

        loaded = (await db_session.execute(select(PrinterGroup).where(PrinterGroup.id == group.id))).scalar_one()
        assert sorted(p.name for p in loaded.printers) == ["A", "B"]

    async def test_printer_can_belong_to_several_groups(self, db_session):
        a = await _mk_printer(db_session, "A")
        g1 = await _mk_group(db_session, "PLA", [a.id])
        g2 = await _mk_group(db_session, "Back room", [a.id])

        rows = (
            (
                await db_session.execute(
                    select(printer_group_members.c.group_id).where(printer_group_members.c.printer_id == a.id)
                )
            )
            .scalars()
            .all()
        )
        assert sorted(rows) == sorted([g1.id, g2.id])

    async def test_queue_item_eager_loads_its_group(self, db_session):
        a = await _mk_printer(db_session, "A")
        group = await _mk_group(db_session, "Farm", [a.id])

        db_session.add(PrintQueueItem(target_group_id=group.id, position=1, status="pending"))
        await db_session.commit()
        db_session.expire_all()

        item = (await db_session.execute(select(PrintQueueItem))).scalars().first()
        assert item.target_group is not None
        assert item.target_group.name == "Farm"


class TestPrinterGroupRoutes:
    async def test_create_list_and_fetch(self, async_client, db_session):
        printer = await _mk_printer(db_session, "Voron01", model="voron_2.4_350")
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/printer-groups",
            json={"name": "PLA Farm", "color": "#4F8A6D", "printer_ids": [printer.id]},
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["printer_count"] == 1
        assert created["printers"][0]["name"] == "Voron01"

        listed = (await async_client.get("/api/v1/printer-groups")).json()
        assert [g["name"] for g in listed] == ["PLA Farm"]

        fetched = (await async_client.get(f"/api/v1/printer-groups/{created['id']}")).json()
        assert fetched["color"] == "#4F8A6D"

    async def test_duplicate_name_is_rejected_case_insensitively(self, async_client, db_session):
        await async_client.post("/api/v1/printer-groups", json={"name": "Farm"})
        resp = await async_client.post("/api/v1/printer-groups", json={"name": "  farm  "})
        assert resp.status_code == 409

    async def test_unknown_printer_id_is_rejected(self, async_client):
        resp = await async_client.post("/api/v1/printer-groups", json={"name": "Ghosts", "printer_ids": [4242]})
        assert resp.status_code == 400
        assert "4242" in resp.json()["detail"]

    async def test_add_and_remove_members(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        await db_session.commit()

        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm"})).json()["id"]

        added = (await async_client.post(f"/api/v1/printer-groups/{gid}/printers/{a.id}")).json()
        assert added["printer_count"] == 1

        # Adding the same printer twice must not create a duplicate row.
        again = (await async_client.post(f"/api/v1/printer-groups/{gid}/printers/{a.id}")).json()
        assert again["printer_count"] == 1

        added_b = (await async_client.post(f"/api/v1/printer-groups/{gid}/printers/{b.id}")).json()
        assert added_b["printer_count"] == 2

        removed = (await async_client.delete(f"/api/v1/printer-groups/{gid}/printers/{a.id}")).json()
        assert [p["name"] for p in removed["printers"]] == ["B"]

    async def test_update_replaces_membership(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        await db_session.commit()

        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm", "printer_ids": [a.id]})).json()[
            "id"
        ]

        updated = (
            await async_client.put(
                f"/api/v1/printer-groups/{gid}",
                json={"description": "night shift", "printer_ids": [b.id]},
            )
        ).json()
        assert updated["description"] == "night shift"
        assert [p["name"] for p in updated["printers"]] == ["B"]

    async def test_update_without_printer_ids_leaves_membership_alone(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        await db_session.commit()
        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm", "printer_ids": [a.id]})).json()[
            "id"
        ]

        updated = (await async_client.put(f"/api/v1/printer-groups/{gid}", json={"color": "#123456"})).json()
        assert updated["printer_count"] == 1

    async def test_delete_refuses_while_pending_items_target_the_group(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        await db_session.commit()
        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm", "printer_ids": [a.id]})).json()[
            "id"
        ]

        db_session.add(PrintQueueItem(target_group_id=gid, position=1, status="pending"))
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/printer-groups/{gid}")
        assert resp.status_code == 409
        assert "pending queue item" in resp.json()["detail"]

    async def test_delete_succeeds_once_nothing_pending(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        await db_session.commit()
        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm", "printer_ids": [a.id]})).json()[
            "id"
        ]

        db_session.add(PrintQueueItem(target_group_id=gid, position=1, status="completed"))
        await db_session.commit()

        assert (await async_client.delete(f"/api/v1/printer-groups/{gid}")).status_code == 204
        assert (await async_client.get(f"/api/v1/printer-groups/{gid}")).status_code == 404


class TestQueueGroupTargeting:
    async def test_group_and_printer_are_mutually_exclusive(self, async_client, db_session):
        a = await _mk_printer(db_session, "A")
        await db_session.commit()
        gid = (await async_client.post("/api/v1/printer-groups", json={"name": "Farm", "printer_ids": [a.id]})).json()[
            "id"
        ]

        resp = await async_client.post(
            "/api/v1/queue/",
            json={"printer_id": a.id, "target_group_id": gid, "archive_id": 1},
        )
        assert resp.status_code == 400
        assert "target_group_id" in resp.json()["detail"]

    async def test_unknown_group_is_rejected(self, async_client):
        resp = await async_client.post("/api/v1/queue/", json={"target_group_id": 4242, "archive_id": 1})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Printer group not found"

    async def test_group_with_no_active_printers_is_rejected(self, async_client, db_session):
        a = await _mk_printer(db_session, "Retired", active=False)
        await db_session.commit()
        gid = (
            await async_client.post("/api/v1/printer-groups", json={"name": "Retired kit", "printer_ids": [a.id]})
        ).json()["id"]

        resp = await async_client.post("/api/v1/queue/", json={"target_group_id": gid, "archive_id": 1})
        assert resp.status_code == 400
        assert "no active printers" in resp.json()["detail"]


class TestSchedulerGroupAssignment:
    """The group branch must match the model branch, since they share a body."""

    @pytest.fixture
    def scheduler(self, monkeypatch):
        # printer 1 idle and connected, 2 printing, 3 offline
        states = {"A": "idle", "B": "printing", "C": "offline"}
        sched = PrintScheduler()

        class FakeManager:
            def __init__(self, id_to_name):
                self._names = id_to_name

            def is_connected(self, pid):
                return states.get(self._names.get(pid)) != "offline"

            def get_status(self, pid):
                return None

            def is_awaiting_plate_clear(self, pid):
                return False

        sched._fake_states = states
        sched._FakeManager = FakeManager
        return sched

    def _install(self, monkeypatch, scheduler, id_to_name):
        manager = scheduler._FakeManager(id_to_name)
        monkeypatch.setattr("backend.app.services.print_scheduler.printer_manager", manager)
        monkeypatch.setattr(
            scheduler,
            "_is_printer_idle",
            lambda pid, require_plate_clear=True: scheduler._fake_states.get(id_to_name.get(pid)) == "idle",
        )

    async def test_picks_the_idle_member(self, db_session, scheduler, monkeypatch):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        c = await _mk_printer(db_session, "C")
        group = await _mk_group(db_session, "Farm", [a.id, b.id, c.id])
        self._install(monkeypatch, scheduler, {a.id: "A", b.id: "B", c.id: "C"})

        printer_id, reason = await scheduler._find_idle_printer_for_group(db_session, group.id, set())
        assert printer_id == a.id
        assert reason is None

    async def test_waiting_reason_names_busy_and_offline_members(self, db_session, scheduler, monkeypatch):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        c = await _mk_printer(db_session, "C")
        group = await _mk_group(db_session, "Farm", [a.id, b.id, c.id])
        self._install(monkeypatch, scheduler, {a.id: "A", b.id: "B", c.id: "C"})

        # A is already claimed by another job in this scheduling pass.
        printer_id, reason = await scheduler._find_idle_printer_for_group(db_session, group.id, {a.id})
        assert printer_id is None
        assert reason == "Busy: A, B | Offline: C"

    async def test_inactive_members_are_not_candidates(self, db_session, scheduler, monkeypatch):
        a = await _mk_printer(db_session, "A", active=False)
        group = await _mk_group(db_session, "Farm", [a.id])
        self._install(monkeypatch, scheduler, {a.id: "A"})

        printer_id, reason = await scheduler._find_idle_printer_for_group(db_session, group.id, set())
        assert printer_id is None
        assert reason == "No active printers in group 'Farm' configured"

    async def test_missing_group_reports_rather_than_raising(self, db_session, scheduler):
        printer_id, reason = await scheduler._find_idle_printer_for_group(db_session, 4242, set())
        assert printer_id is None
        assert reason == "Target printer group no longer exists"

    async def test_model_branch_is_unchanged_by_the_refactor(self, db_session, scheduler, monkeypatch):
        a = await _mk_printer(db_session, "A")
        b = await _mk_printer(db_session, "B")
        c = await _mk_printer(db_session, "C")
        await db_session.commit()
        self._install(monkeypatch, scheduler, {a.id: "A", b.id: "B", c.id: "C"})

        printer_id, reason = await scheduler._find_idle_printer_for_model(db_session, "H2C", set())
        assert printer_id == a.id and reason is None

        printer_id, reason = await scheduler._find_idle_printer_for_model(db_session, "H2C", {a.id})
        assert printer_id is None
        assert reason == "Busy: A, B | Offline: C"

        printer_id, reason = await scheduler._find_idle_printer_for_model(db_session, "X1C", set())
        assert printer_id is None
        assert reason == "No active X1C printers configured"


class TestSameTarget:
    """SJF starvation guard may only mark items competing for the same set."""

    def test_same_group(self):
        assert PrintScheduler._same_target(PrintQueueItem(target_group_id=1), PrintQueueItem(target_group_id=1))

    def test_different_group(self):
        assert not PrintScheduler._same_target(PrintQueueItem(target_group_id=2), PrintQueueItem(target_group_id=1))

    def test_same_model_case_insensitive(self):
        assert PrintScheduler._same_target(PrintQueueItem(target_model="h2c"), PrintQueueItem(target_model="H2C"))

    def test_group_and_model_never_match(self):
        assert not PrintScheduler._same_target(PrintQueueItem(target_group_id=1), PrintQueueItem(target_model="H2C"))

    def test_untargeted_items_never_match(self):
        assert not PrintScheduler._same_target(PrintQueueItem(), PrintQueueItem())
