"""G-code upload to Elegoo printers.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware; see docs/elegoo-printers.md.

SDCP v3 (Centauri Carbon): ``POST http://<ip>:3030/uploadFile/upload``,
multipart, 1 MB packets, each with ``S-File-MD5`` (MD5 of the whole file),
``Check=1``, ``Offset``, ``Uuid`` (same for every packet), ``TotalSize`` and
``File``. Success is ``{"code": "000000", "success": true}``.

Centauri Carbon 2: ``PUT http://<ip>:80/upload`` in 1 MB chunks with
``Content-Range``, ``X-File-Name``, ``X-File-MD5`` and ``X-Token`` (the access
code, ``123456`` when none is set) over ONE kept-alive connection. Firmware
02.01.00.00 answers a new connection per chunk with HTTP 429 and a retried
chunk corrupts the file, so nothing is retried (measured by the
elegoo-homeassistant project).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import httpx

CHUNK_SIZE = 1024 * 1024
CC2_UPLOAD_PORT = 80
CC2_DEFAULT_TOKEN = "123456"


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - the printer's integrity check, not security
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


async def upload_sdcp(ip: str, port: int, file_path: str, remote_name: str) -> None:
    path = Path(file_path)
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"{remote_name} is empty")
    md5 = _md5(path)
    upload_id = uuid.uuid4().hex
    url = f"http://{ip}:{port}/uploadFile/upload"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        with path.open("rb") as fh:
            offset = 0
            while chunk := fh.read(CHUNK_SIZE):
                data = {
                    "S-File-MD5": md5,
                    "Check": "1",
                    "Offset": str(offset),
                    "Uuid": upload_id,
                    "TotalSize": str(size),
                }
                files = {"File": (remote_name, chunk, "application/octet-stream")}
                resp = await client.post(url, data=data, files=files)
                resp.raise_for_status()
                body = resp.json() if resp.content else {}
                if not (body.get("success") or body.get("code") == "000000"):
                    raise RuntimeError(f"printer refused packet at byte {offset}: {body.get('messages')}")
                offset += len(chunk)


async def upload_cc2(ip: str, file_path: str, remote_name: str, token: str | None = None) -> None:
    path = Path(file_path)
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"{remote_name} is empty")
    md5 = _md5(path)
    url = f"http://{ip}:{CC2_UPLOAD_PORT}/upload"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        with path.open("rb") as fh:
            offset = 0
            while chunk := fh.read(CHUNK_SIZE):
                end = offset + len(chunk) - 1
                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                    "X-File-Name": remote_name,
                    "X-File-MD5": md5,
                    "X-Token": token or CC2_DEFAULT_TOKEN,
                    "Accept": "application/json",
                }
                resp = await client.put(url, content=chunk, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code} at byte {offset} (not retried)")
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if body.get("error_code") != 0:
                    raise RuntimeError(f"printer refused chunk at byte {offset}: error_code {body.get('error_code')}")
                offset += len(chunk)
