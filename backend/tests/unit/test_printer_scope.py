"""Unit tests for per-printer permission scoping (#57): the combination rules,
the scheduler's candidate filter, browser push filtering and the migration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text

from backend.app.core.printer_scope import PrinterScope, normalize_location, scope_for_user


def _group(name="g", ids=None, locs=None):
    return SimpleNamespace(name=name, scope_printer_ids=ids, scope_locations=locs)


def _user(groups, role="user", uid=7):
    user = SimpleNamespace(id=uid, role=role, groups=groups)
    user.is_admin = role == "admin" or any(g.name == "Administrators" for g in groups)
    return user


class TestScopeForUser:
    def test_unscoped_group_means_full_access(self):
        assert scope_for_user(_user([_group()])) is None

    def test_no_groups_means_unscoped(self):
        assert scope_for_user(_user([])) is None

    def test_none_user_is_unscoped(self):
        assert scope_for_user(None) is None

    def test_admin_bypasses_scope(self):
        assert scope_for_user(_user([_group(ids=[1])], role="admin")) is None
        assert scope_for_user(_user([_group("Administrators"), _group(ids=[1])])) is None

    def test_union_of_scoped_groups(self):
        scope = scope_for_user(_user([_group(ids=[1, 2]), _group(ids=[3], locs=["Bay 1"]), _group(locs=["bay 2 "])]))
        assert scope == PrinterScope(
            user_id=7, printer_ids=frozenset({1, 2, 3}), locations=frozenset({"bay 1", "bay 2"})
        )

    def test_any_unscoped_group_wins(self):
        assert scope_for_user(_user([_group(ids=[1]), _group()])) is None

    def test_empty_scope_grants_nothing(self):
        scope = scope_for_user(_user([_group(ids=[], locs=None)]))
        assert scope is not None
        assert not scope.allows(1, "anywhere")

    def test_allows_by_id_or_location(self):
        scope = PrinterScope(user_id=1, printer_ids=frozenset({5}), locations=frozenset({"bay 1"}))
        assert scope.allows(5, None)
        assert scope.allows(9, "  Bay 1")
        assert not scope.allows(9, "Bay 2")
        assert not scope.allows(9, None)

    def test_normalize_location(self):
        assert normalize_location("  Back Room ") == "back room"
        assert normalize_location("   ") is None
        assert normalize_location(None) is None


class TestSchedulerCandidates:
    async def test_model_candidates_limited_to_creator_scope(self, db_session):
        from backend.app.models.printer import Printer
        from backend.app.services.print_scheduler import PrintScheduler

        a = Printer(name="a", serial_number="S-A", ip_address="10.0.0.1", access_code="x", model="X1C")
        b = Printer(name="b", serial_number="S-B", ip_address="10.0.0.2", access_code="x", model="X1C")
        db_session.add_all([a, b])
        await db_session.commit()

        sched = PrintScheduler()
        seen: list[list[int]] = []

        def fake_select(printers, *args, **kwargs):
            seen.append(sorted(p.id for p in printers))
            return None, "busy"

        with patch.object(sched, "_select_idle_printer", side_effect=fake_select):
            await sched._find_idle_printer_for_model(db_session, "X1C", set(), allowed_ids={b.id})
            await sched._find_idle_printer_for_model(db_session, "X1C", set(), allowed_ids=None)
        assert seen == [[b.id], sorted([a.id, b.id])]

    async def test_group_candidates_limited_to_creator_scope(self, db_session):
        from backend.app.models.printer import Printer
        from backend.app.models.printer_group import PrinterGroup, printer_group_members
        from backend.app.services.print_scheduler import PrintScheduler

        a = Printer(name="a", serial_number="S-GA", ip_address="10.0.1.1", access_code="x", model="X1C")
        b = Printer(name="b", serial_number="S-GB", ip_address="10.0.1.2", access_code="x", model="P1S")
        grp = PrinterGroup(name="G", name_key="g")
        db_session.add_all([a, b, grp])
        await db_session.commit()
        await db_session.execute(
            printer_group_members.insert(),
            [{"group_id": grp.id, "printer_id": a.id}, {"group_id": grp.id, "printer_id": b.id}],
        )
        await db_session.commit()

        sched = PrintScheduler()
        seen: list[list[int]] = []

        def fake_select(printers, *args, **kwargs):
            seen.append(sorted(p.id for p in printers))
            return None, "busy"

        with patch.object(sched, "_select_idle_printer", side_effect=fake_select):
            reason = await sched._find_idle_printer_for_group(db_session, grp.id, set(), allowed_ids=set())
            await sched._find_idle_printer_for_group(db_session, grp.id, set(), allowed_ids={a.id})
        assert reason[0] is None and "No active" in reason[1]
        assert seen == [[a.id]]


class TestWebPushFilter:
    async def test_scoped_subscriber_skipped_for_hidden_printer(self, db_session):
        from backend.app.models.group import Group
        from backend.app.models.printer import Printer
        from backend.app.models.push_subscription import PushSubscription
        from backend.app.models.user import User
        from backend.app.services import webpush

        p1 = Printer(name="p1", serial_number="S-W1", ip_address="10.0.2.1", access_code="x")
        p2 = Printer(name="p2", serial_number="S-W2", ip_address="10.0.2.2", access_code="x")
        db_session.add_all([p1, p2])
        await db_session.commit()
        grp = Group(name="Scoped push", permissions=[], scope_printer_ids=[p1.id])
        user = User(username="pushy", role="user")
        db_session.add_all([grp, user])
        await db_session.commit()
        await db_session.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g)"), {"u": user.id, "g": grp.id}
        )
        db_session.add_all(
            [
                PushSubscription(user_id=user.id, endpoint="https://push.example/scoped", p256dh="k", auth="a"),
                PushSubscription(user_id=None, endpoint="https://push.example/anyone", p256dh="k", auth="a"),
            ]
        )
        await db_session.commit()

        sent: list[str] = []

        def fake_send(sub, payload, pem):
            sent.append(sub.endpoint)
            return None

        async def fake_keys(_db):
            return "pem", "pub"

        with patch.object(webpush, "_send_one", side_effect=fake_send), patch.object(webpush, "vapid_keys", fake_keys):
            await webpush.send_all("t", "m", db=db_session, printer_id=p2.id)
            assert sent == ["https://push.example/anyone"]
            sent.clear()
            await webpush.send_all("t", "m", db=db_session, printer_id=p1.id)
            assert sorted(sent) == ["https://push.example/anyone", "https://push.example/scoped"]


LEGACY_GROUPS = """
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500),
    permissions JSON,
    is_system BOOLEAN DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class TestMigration:
    @pytest.fixture(autouse=True)
    def force_sqlite_dialect(self, monkeypatch):
        from backend.app.core import database as database_module, db_dialect

        monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
        monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
        monkeypatch.setattr(database_module, "is_sqlite", lambda: True)

    @pytest.fixture
    async def legacy_engine(self):
        """The trunk schema on SQLite: groups without the scope columns, holding two groups.

        Its own SQLite engine (the migration under test is the SQLite branch; the
        PostgreSQL statements are pinned by test_pg_branch_statements below)."""
        from sqlalchemy.ext.asyncio import create_async_engine

        import backend.app.models  # noqa: F401  (registers every table)
        from backend.app.core.database import Base

        test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with test_engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
            await conn.execute(text("DROP TABLE user_groups"))
            await conn.execute(text("DROP TABLE groups"))
            await conn.execute(text(LEGACY_GROUPS))
            await conn.execute(
                text(
                    "CREATE TABLE user_groups (user_id INTEGER NOT NULL, group_id INTEGER NOT NULL, "
                    "PRIMARY KEY (user_id, group_id))"
                )
            )
            await conn.execute(
                text("INSERT INTO groups (id, name, permissions, is_system) VALUES (:i, :n, :p, :s)"),
                [
                    {"i": 1, "n": "Operators", "p": '["printers:read"]', "s": 1},
                    {"i": 2, "n": "Night shift", "p": "[]", "s": 0},
                ],
            )
        yield test_engine
        await test_engine.dispose()

    async def test_pg_branch_statements(self):
        """On PostgreSQL the columns are added with IF NOT EXISTS (no PRAGMA-style retry there)."""
        from unittest.mock import AsyncMock, MagicMock

        from backend.app.core import database as db_module

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        executed: list[str] = []

        async def fake_safe_execute(_conn, sql: str) -> None:
            executed.append(sql)

        fake_conn = MagicMock()
        fake_conn.begin_nested = lambda: _Ctx()
        fake_conn.execute = AsyncMock(return_value=MagicMock(fetchone=MagicMock(return_value=None)))
        with (
            patch("backend.app.core.database.is_sqlite", return_value=False),
            patch("backend.app.core.database._safe_execute", side_effect=fake_safe_execute),
            patch("backend.app.core.database._migrate_update_auto_link_constraint", AsyncMock()),
            patch("backend.app.core.database._migrate_widen_spoolman_slot_ams_id_range", AsyncMock()),
        ):
            await db_module.run_migrations(fake_conn)
        stmts = [x for x in executed if "scope_printer_ids" in x or "scope_locations" in x or "scope_user_id" in x]
        assert stmts == [
            "ALTER TABLE groups ADD COLUMN IF NOT EXISTS scope_printer_ids JSON",
            "ALTER TABLE groups ADD COLUMN IF NOT EXISTS scope_locations JSON",
            "ALTER TABLE auth_ephemeral_tokens ADD COLUMN IF NOT EXISTS scope_user_id INTEGER",
        ]

    async def test_upgrade_leaves_every_group_unscoped(self, legacy_engine):
        from backend.app.core.database import run_migrations

        async with legacy_engine.begin() as conn:
            cols = {row[1] for row in await conn.execute(text("PRAGMA table_info(groups)"))}
        assert "scope_printer_ids" not in cols

        async with legacy_engine.begin() as conn:
            await run_migrations(conn)
        async with legacy_engine.begin() as conn:
            await run_migrations(conn)  # idempotent

        async with legacy_engine.begin() as conn:
            rows = (await conn.execute(text("SELECT name, scope_printer_ids, scope_locations FROM groups"))).all()
            token_cols = {row[1] for row in await conn.execute(text("PRAGMA table_info(auth_ephemeral_tokens)"))}
        assert len(rows) == 2
        assert all(r[1] is None and r[2] is None for r in rows)
        assert "scope_user_id" in token_cols

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from sqlalchemy.orm import selectinload

        from backend.app.models.group import Group

        maker = async_sessionmaker(legacy_engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            from sqlalchemy import select

            groups = (await session.execute(select(Group).options(selectinload(Group.users)))).scalars().all()
        user = SimpleNamespace(id=1, role="user", groups=groups, is_admin=False)
        assert scope_for_user(user) is None
