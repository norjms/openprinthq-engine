"""Bambu dispatch must reject a raw .gcode library file before it ever reaches
FTP — derive_remote_filename() would otherwise append ".3mf" and ship a file
that LOOKS like a .gcode.3mf container but whose body is plain gcode text,
triggering a printer-side firmware parse failure ~30s into the print (#1401).

This guard replaces the old upload-time rejection in library.py, which
blanket-blocked every raw .gcode upload — including the ones the Klipper/
OctoPrint/Duet/FlashForge/MKS/Obico transports require."""

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture
async def bambu_queue_item(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def make(filename: str):
        base_dir = tmp_path / filename.replace(".", "_")
        (base_dir / "library").mkdir(parents=True)
        source_path = base_dir / "library" / filename
        source_path.write_bytes(b"; raw gcode\nG28\n")

        async with session_maker() as db:
            printer = Printer(
                name="Bambu Printer",
                serial_number="SERIAL-1",
                ip_address="127.0.0.1",
                access_code="access-code",
                model="X1C",
            )
            library_file = LibraryFile(
                filename=filename,
                file_path=str(source_path),
                file_type=filename.rsplit(".", 1)[-1],
                file_size=source_path.stat().st_size,
                file_hash=None,
            )
            db.add_all([printer, library_file])
            await db.flush()

            item = PrintQueueItem(
                printer_id=printer.id,
                library_file_id=library_file.id,
                status="pending",
                bed_levelling=True,
                flow_cali=False,
                vibration_cali=True,
                layer_inspect=False,
                timelapse=False,
                use_ams=True,
                nozzle_offset_cali=True,
            )
            db.add(item)
            await db.commit()

            return SimpleNamespace(
                session_maker=session_maker,
                base_dir=base_dir,
                queue_item_id=item.id,
                upload=AsyncMock(return_value=True),
                start_print=MagicMock(return_value=True),
            )

    try:
        yield make
    finally:
        await engine.dispose()


async def _dispatch(ctx):
    scheduler = PrintScheduler()

    async def archive_print(self, *, printer_id, source_file, original_filename, created_by_id=None, project_id=None):
        from backend.app.models.archive import PrintArchive

        archive_rel_path = Path("archives") / f"archive-{ctx.queue_item_id}{Path(original_filename).suffix}"
        archive_path = ctx.base_dir / archive_rel_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(Path(source_file).read_bytes())

        archive = PrintArchive(
            printer_id=printer_id,
            filename=original_filename,
            file_path=str(archive_rel_path),
            file_size=archive_path.stat().st_size,
            content_hash=None,
            thumbnail_path=None,
            timelapse_path=None,
            print_time_seconds=120,
            status="completed",
            project_id=project_id,
            created_by_id=created_by_id,
        )
        self.db.add(archive)
        await self.db.flush()
        return archive

    patches = [
        patch.object(scheduler_module.settings, "base_dir", ctx.base_dir),
        patch("backend.app.services.archive.ArchiveService.archive_print", new=archive_print),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=None)),
        patch("backend.app.services.print_scheduler.printer_manager.start_print", ctx.start_print),
        patch("backend.app.services.print_scheduler.upload_file_async", ctx.upload),
        patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
        patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
    ]
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        async with ctx.session_maker() as db:
            item = await db.get(PrintQueueItem, ctx.queue_item_id)
            await scheduler._start_print(db, item)

    async with ctx.session_maker() as db:
        return await db.get(PrintQueueItem, ctx.queue_item_id)


@pytest.mark.asyncio
async def test_bambu_dispatch_rejects_raw_gcode(bambu_queue_item):
    ctx = await bambu_queue_item("model.gcode")

    item = await _dispatch(ctx)

    assert item.status == "failed"
    assert "gcode.3mf" in item.error_message
    ctx.upload.assert_not_awaited()
    ctx.start_print.assert_not_called()


@pytest.mark.asyncio
async def test_bambu_dispatch_accepts_gcode_3mf(bambu_queue_item):
    ctx = await bambu_queue_item("model.gcode.3mf")

    item = await _dispatch(ctx)

    assert item.status == "printing"
    ctx.upload.assert_awaited()
