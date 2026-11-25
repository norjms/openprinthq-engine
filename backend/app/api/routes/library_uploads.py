"""Resumable (chunked) library uploads (OpenPrintHQ farm feature).

One request per file breaks on large 3MF and G-code files behind proxies with
body limits and on flaky links, and there is no way to resume. These routes
upload a file in pieces:

1. ``POST /library/uploads`` with ``{"filename", "total_size", "folder_id",
   "generate_stl_thumbnails"}`` starts a session and returns its ``id``;
2. ``PUT /library/uploads/{id}?offset=N`` with the raw bytes of the next piece
   (``offset`` must equal the bytes received so far; a client that lost track
   asks ``GET /library/uploads/{id}`` and continues from ``received``);
3. ``POST /library/uploads/{id}/complete`` once every byte is there; the file
   then goes through exactly the same checks and processing as a normal
   ``POST /library/files`` upload, and the answer is the same.

Sessions expire after 24 hours; their partial files are removed when a new
session starts. ``DELETE /library/uploads/{id}`` abandons one.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import require_permission_if_auth_enabled
from backend.app.core.config import settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.upload_session import UploadSession
from backend.app.models.user import User
from backend.app.utils.filename import InvalidFilenameError, validate_print_filename

router = APIRouter(prefix="/library/uploads", tags=["library"])

MAX_TOTAL = 8 * 1024**3  # 8 GiB per file
MAX_PIECE = 64 * 1024**2  # 64 MiB per request
SESSION_TTL = timedelta(hours=24)

_upload = require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)


def _dir() -> Path:
    d = Path(app_settings.base_dir) / "uploads" / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _part(session_id: str) -> Path:
    return _dir() / f"{session_id}.part"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StartBody(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    total_size: int = Field(..., ge=1, le=MAX_TOTAL)
    folder_id: int | None = None
    generate_stl_thumbnails: bool = True


class SessionOut(BaseModel):
    id: str
    filename: str
    total_size: int
    received: int
    max_piece: int = MAX_PIECE
    expires_at: datetime


def _out(s: UploadSession) -> SessionOut:
    return SessionOut(
        id=s.id, filename=s.filename, total_size=s.total_size, received=s.received, expires_at=s.expires_at
    )


async def _get(db: AsyncSession, session_id: str, user: User | None) -> UploadSession:
    s = await db.get(UploadSession, session_id)
    if s is None or s.expires_at < _now():
        raise HTTPException(404, "Upload session not found or expired")
    if user is not None and s.user_id is not None and s.user_id != user.id:
        raise HTTPException(404, "Upload session not found or expired")
    return s


async def _cleanup_expired(db: AsyncSession) -> None:
    for s in (await db.execute(select(UploadSession).where(UploadSession.expires_at < _now()))).scalars().all():
        _part(s.id).unlink(missing_ok=True)
        await db.delete(s)
    await db.commit()


@router.post("", response_model=SessionOut, status_code=201)
async def start_upload(body: StartBody, db: AsyncSession = Depends(get_db), user: User | None = Depends(_upload)):
    try:
        validate_print_filename(body.filename)
    except InvalidFilenameError as e:
        raise HTTPException(400, str(e)) from e
    if os.path.basename(body.filename) != body.filename:
        raise HTTPException(400, "Filename must not contain a path")
    await _cleanup_expired(db)
    s = UploadSession(
        id=str(uuid.uuid4()),
        user_id=user.id if user is not None else None,
        filename=body.filename,
        total_size=body.total_size,
        received=0,
        folder_id=body.folder_id,
        generate_stl_thumbnails=body.generate_stl_thumbnails,
        expires_at=_now() + SESSION_TTL,
    )
    db.add(s)
    await db.commit()
    _part(s.id).write_bytes(b"")
    return _out(s)


@router.get("/{session_id}", response_model=SessionOut)
async def upload_status(session_id: str, db: AsyncSession = Depends(get_db), user: User | None = Depends(_upload)):
    return _out(await _get(db, session_id, user))


@router.put("/{session_id}", response_model=SessionOut)
async def upload_piece(
    session_id: str,
    request: Request,
    offset: int = Query(..., ge=0),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(_upload),
):
    s = await _get(db, session_id, user)
    if offset != s.received:
        raise HTTPException(409, f"Expected offset {s.received}")
    part = _part(s.id)
    written = 0
    with part.open("r+b") as f:
        f.seek(s.received)
        f.truncate()
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_PIECE or s.received + written > s.total_size:
                f.truncate(s.received)
                raise HTTPException(413, "Piece too large or past the declared size")
            f.write(chunk)
    s.received += written
    await db.commit()
    return _out(s)


@router.post("/{session_id}/complete")
async def complete_upload(session_id: str, db: AsyncSession = Depends(get_db), user: User | None = Depends(_upload)):
    from backend.app.api.routes.library import upload_file

    s = await _get(db, session_id, user)
    if s.received != s.total_size:
        raise HTTPException(409, f"Upload incomplete: {s.received} of {s.total_size} bytes")
    part = _part(s.id)
    try:
        with part.open("rb") as fh:
            result = await upload_file(
                file=UploadFile(file=fh, filename=s.filename, size=s.total_size),
                folder_id=s.folder_id,
                generate_stl_thumbnails=s.generate_stl_thumbnails,
                db=db,
                current_user=user,
            )
    finally:
        part.unlink(missing_ok=True)
        await db.delete(s)
        await db.commit()
    return result


@router.delete("/{session_id}")
async def abandon_upload(session_id: str, db: AsyncSession = Depends(get_db), user: User | None = Depends(_upload)):
    s = await _get(db, session_id, user)
    _part(s.id).unlink(missing_ok=True)
    await db.delete(s)
    await db.commit()
    return {"success": True}
