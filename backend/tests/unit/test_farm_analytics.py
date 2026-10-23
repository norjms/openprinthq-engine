"""Farm analytics: state buckets, intervals on change, and the report."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.services import farm_analytics as fa


def test_buckets():
    st = lambda s, c=True: SimpleNamespace(state=s, connected=c)  # noqa: E731
    assert fa.bucket_of(st("RUNNING"), False) == "printing"
    assert fa.bucket_of(st("PAUSE"), False) == "paused"
    assert fa.bucket_of(st("FINISH"), True) == "waiting_plate"
    assert fa.bucket_of(st("IDLE"), False) == "idle"
    assert fa.bucket_of(st("IDLE", False), False) == "offline"
    assert fa.bucket_of(None, False) == "offline"


@pytest.fixture
def wired(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(fa.database, "async_session", maker)
    fa._current.clear()
    yield maker
    fa._current.clear()


async def test_intervals_and_report(wired):
    t0 = datetime(2026, 10, 20, 8, 0, 0)
    run = SimpleNamespace(state="RUNNING", connected=True)
    fin = SimpleNamespace(state="FINISH", connected=True)
    idle = SimpleNamespace(state="IDLE", connected=True)
    await fa.observe(1, idle, False, now=t0)
    await fa.observe(1, run, False, now=t0 + timedelta(hours=1))
    await fa.observe(1, run, False, now=t0 + timedelta(hours=2))  # same bucket: nothing written
    await fa.observe(1, fin, True, now=t0 + timedelta(hours=4))
    await fa.observe(1, idle, False, now=t0 + timedelta(hours=5))
    async with wired() as db:
        rep = await fa.report(db, days=1, now=t0 + timedelta(hours=6))
    p = rep["printers"][0]
    assert p["printer_id"] == 1
    assert p["hours"]["printing"] == 3.0 and p["hours"]["waiting_plate"] == 1.0
    assert p["hours"]["idle"] == 2.0  # 1 h before plus 1 h still open
    assert p["utilization"] == 0.5
    assert rep["farm"]["hours"]["printing"] == 3.0
    assert len(rep["completed_per_day"]) == 2


async def test_route(async_client):
    r = await async_client.get("/api/v1/stats/farm?days=3")
    assert r.status_code == 200 and r.json()["days"] == 3
