"""Upload g-code to an MKS-WiFi printer.

Matches OrcaSlicer's ``MKS`` host exactly: the upload is plain HTTP
(``POST http://<ip>/upload?X-Filename=<name>``, always port 80 — this isn't
configurable in the MKS firmware/protocol), then start is two commands on the
TCP console (``M23 <name>``, ``M24``) after a short settle delay the firmware
needs before it responds to g-code following an upload.
"""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0
_POST_UPLOAD_SETTLE_SECONDS = 1.5  # firmware needs a beat before it accepts gcode


async def upload_gcode(
    ip_address: str,
    port: int,  # TCP console port, used only for the post-upload start commands
    local_path: str,
    *,
    remote_name: str | None = None,
    password: str | None = None,  # unused — no auth on this transport
    use_https: bool = False,  # unused — MKS upload is always plain HTTP
    start_print: bool = False,
) -> str:
    """Upload a g-code file to an MKS printer. Returns the remote filename."""
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)

    with open(local_path, "rb") as fh:
        body = fh.read()

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"http://{ip_address}/upload?X-Filename={quote(name)}",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()
        try:
            err = (resp.json() or {}).get("err", 0)
        except ValueError:
            err = 0
        if err:
            raise RuntimeError(f"MKS upload reported error code {err}")

    if start_print:
        from backend.app.services.mks.mks_client import _COMMAND_TIMEOUT_SECONDS

        await asyncio.sleep(_POST_UPLOAD_SETTLE_SECONDS)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip_address, port), timeout=10.0)
        try:
            for cmd in (f"M23 {name}", "M24"):
                writer.write(f"{cmd}\n".encode())
                await writer.drain()

                async def _read_until_ok() -> None:
                    while True:
                        raw = await reader.readline()
                        if not raw:
                            raise ConnectionError("connection closed by peer")
                        if raw.decode(errors="replace").strip().lower() == "ok":
                            return

                await asyncio.wait_for(_read_until_ok(), timeout=_COMMAND_TIMEOUT_SECONDS)
        finally:
            writer.close()

    logger.info("MKS upload OK: %s -> %s (%s)", local_path, name, ip_address)
    return name
