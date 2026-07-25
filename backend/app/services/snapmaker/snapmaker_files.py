"""Upload g-code to a Snapmaker printer and start it. Added by OpenPrintHQ
(2026-07-25), AGPL-3.0.

Snapmaker's HTTP API accepts a multipart ``POST /api/v1/upload`` (field
``file``) carrying the auth token; the printer stores and begins printing it. A
token comes from ``POST /api/v1/connect`` (the user authorizes it once on the
touchscreen).
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0


async def _obtain_token(client: httpx.AsyncClient, base: str) -> str | None:
    r = await client.post(f"{base}/connect", data={})
    r.raise_for_status()
    return (r.json() or {}).get("token")


async def upload_gcode(
    ip_address: str,
    port: int,
    local_path: str,
    *,
    remote_name: str | None = None,
    token: str | None = None,
    start_print: bool = True,
) -> str:
    """Upload a local g-code file to the Snapmaker; returns the remote filename."""
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)
    base = f"http://{ip_address}:{port or 8080}/api/v1"

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
        tok = token or await _obtain_token(client, base)
        with open(local_path, "rb") as fh:
            files = {"file": (name, fh, "application/octet-stream")}
            data = {"token": tok} if tok else {}
            resp = await client.post(f"{base}/upload", files=files, data=data)
        resp.raise_for_status()

    logger.info("Snapmaker upload OK: %s -> %s (%s)", local_path, name, base)
    return name
