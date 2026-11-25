"""Which database the tests run on (OpenPrintHQ, 2026-10-22).

Default: in-memory SQLite, as before; nothing to set up.

With TEST_DATABASE_URL set to a PostgreSQL URL (postgresql+asyncpg://user:pass@host:5432/anything), the suite runs on
PostgreSQL, which is what OpenPrintHQ runs in production:

- each pytest-xdist worker gets its own database (<name>_<worker>), made fresh at start and dropped at exit;
- each test engine gets its own schema in that database (make_test_engine), so tests never see each other's rows
  and every session gets a real connection of its own, as in production;
- DATABASE_URL is pointed at the worker's database before the app is imported, so the app's dialect branches
  (core/db_dialect.is_sqlite) and its global engine match.

Tests that are about SQLite itself (file databases, SQLite-only migrations) keep their own SQLite URLs.
"""

import asyncio
import os
import re
import uuid
import weakref

SQLITE_MEMORY = "sqlite+aiosqlite:///:memory:"
_SCHEMAS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()  # sync engine -> its test schema
_RAW = os.environ.get("TEST_DATABASE_URL", "").strip()
POSTGRES = _RAW.startswith("postgresql")


def _worker_url() -> str:
    if not POSTGRES:
        return SQLITE_MEMORY
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    base, _, name = _RAW.rpartition("/")
    name = re.sub(r"[^a-z0-9_]", "_", (name.split("?")[0] or "ophq_test").lower())
    return f"{base}/{name}_{worker}"


WORKER_URL = _worker_url()


def _plain(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _admin_url() -> str:
    base, _, _ = WORKER_URL.rpartition("/")
    return f"{base}/postgres"


def _dbname() -> str:
    return WORKER_URL.rpartition("/")[2]


async def _recreate():
    import asyncpg

    conn = await asyncpg.connect(_plain(_admin_url()))
    try:
        name = _dbname()
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop():
    import asyncpg

    conn = await asyncpg.connect(_plain(_admin_url()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_dbname()}" WITH (FORCE)')
    finally:
        await conn.close()


def setup_worker_database():
    """Call once, at conftest import, before the app is imported."""
    if not POSTGRES:
        return
    asyncio.run(_recreate())
    os.environ["DATABASE_URL"] = WORKER_URL


async def _init_app_database():
    from backend.app.core.database import engine, init_db

    try:
        await init_db()
    finally:
        await engine.dispose()  # its connections belong to this short-lived loop


def init_app_database():
    """Give the app's own database (the worker database's public schema) its tables, as the app's startup does.

    Code that opens the app's global session instead of a test's (background helpers, auth checks) then finds
    real, empty tables, as on SQLite. Call after the app's settings are loaded (conftest does it at import)."""
    if not POSTGRES:
        return
    asyncio.run(_init_app_database())
    # Each test runs in its own event loop, and an asyncpg connection belongs to the loop that opened it, so the
    # app's pooled connections would break from the second test on ("attached to a different loop"). The app's
    # session factory gets an engine without a pool: a fresh connection per session, in the current loop.
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.app.core import database

    unpooled = create_async_engine(WORKER_URL, poolclass=NullPool)
    database.install_postgres_listeners(unpooled)
    database.async_session.configure(bind=unpooled)


def drop_worker_database():
    """Call from pytest_sessionfinish (an atexit hook runs too late for asyncpg's DNS lookup)."""
    if not POSTGRES:
        return
    try:
        asyncio.run(_drop())
    except Exception:  # noqa: BLE001 - the next run drops and recreates it anyway
        pass


def make_test_engine(**kw):
    """A fresh, empty database for one test: in-memory SQLite, or a schema of its own on PostgreSQL."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    if not POSTGRES:
        return create_async_engine(SQLITE_MEMORY, **kw)
    schema = "t_" + uuid.uuid4().hex[:16]
    kw.setdefault("pool_size", 10)
    kw.setdefault("max_overflow", 40)
    engine = create_async_engine(WORKER_URL, **kw)

    @event.listens_for(engine.sync_engine, "connect")
    def _use_schema(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        except Exception:  # noqa: BLE001 - another pooled connection made it first
            dbapi_conn.rollback()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()
        dbapi_conn.commit()

    from backend.app.core.database import install_postgres_listeners

    install_postgres_listeners(engine)  # the same tz handling the app's engine has
    _SCHEMAS[engine.sync_engine] = schema
    return engine


def test_schema(engine) -> str | None:
    """The schema make_test_engine gave this engine (PostgreSQL), else None."""
    return _SCHEMAS.get(engine.sync_engine)


async def drop_test_schema(engine):
    """Remove a test engine's schema (PostgreSQL only; SQLite memory databases vanish on their own)."""
    schema = test_schema(engine)
    if not schema:
        return
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
