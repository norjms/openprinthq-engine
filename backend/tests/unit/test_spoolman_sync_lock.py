"""The Spoolman side of on_ams_change runs one at a time per printer.

An MQTT burst delivers several AMS updates for one printer within milliseconds.
Each Spoolman sync reads Spoolman's spools once, before another run's new spool
exists, so concurrent runs each created a spool for the same RFID tag (every
tagged spool on an H2D and an H2C was created twice in the same second).
"""

import asyncio

import pytest

from backend.app import main


@pytest.fixture(autouse=True)
def _isolate_locks_dict():
    saved = dict(main._spoolman_sync_locks)
    main._spoolman_sync_locks.clear()
    try:
        yield
    finally:
        main._spoolman_sync_locks.clear()
        main._spoolman_sync_locks.update(saved)


@pytest.mark.asyncio
async def test_lock_is_per_printer():
    assert main._get_spoolman_sync_lock(7) is main._get_spoolman_sync_lock(7)
    assert main._get_spoolman_sync_lock(7) is not main._get_spoolman_sync_lock(8)


def test_a_lock_held_in_a_finished_loop_does_not_block_the_next():
    async def hold():
        await main._get_spoolman_sync_lock(9).acquire()  # never released: its loop ends

    asyncio.run(hold())

    async def take():
        async with main._get_spoolman_sync_lock(9):
            return True

    assert asyncio.run(asyncio.wait_for(take(), 2))


@pytest.mark.asyncio
async def test_a_burst_of_ams_updates_syncs_to_spoolman_one_at_a_time(monkeypatch):
    inside = 0
    most = 0
    runs: list[int] = []

    async def fake_sync(printer_id, ams_data, logger):
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        await asyncio.sleep(0.02)  # Spoolman answers while the rest of the burst arrives
        runs.append(printer_id)
        inside -= 1

    monkeypatch.setattr(main, "_sync_ams_to_spoolman", fake_sync)
    # Everything else on_ams_change does is best-effort and swallowed when absent.
    await asyncio.gather(*(main.on_ams_change(5, []) for _ in range(3)), main.on_ams_change(6, []))
    assert sorted(runs) == [5, 5, 5, 6]
    assert most <= 2  # printer 6 may overlap printer 5, never printer 5 itself


@pytest.mark.asyncio
async def test_same_printer_never_overlaps(monkeypatch):
    inside = {5: 0}
    most = {5: 0}

    async def fake_sync(printer_id, ams_data, logger):
        inside[printer_id] += 1
        most[printer_id] = max(most[printer_id], inside[printer_id])
        await asyncio.sleep(0.02)
        inside[printer_id] -= 1

    monkeypatch.setattr(main, "_sync_ams_to_spoolman", fake_sync)
    await asyncio.gather(*(main.on_ams_change(5, []) for _ in range(4)))
    assert most[5] == 1
