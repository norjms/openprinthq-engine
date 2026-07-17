"""Upload g-code to a FlashForge legacy printer (raw TCP :8899).

Opens its own short-lived connection (independent of ``FlashForgeClient``'s
polling connection) — logs in, streams the file via ``~M28``/raw
bytes/``~M29``, then optionally starts it with ``~M23``.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 10.0
_COMMAND_TIMEOUT_SECONDS = 10.0
_UPLOAD_TIMEOUT_SECONDS = 300.0


async def _read_until_ok(reader: asyncio.StreamReader) -> str:
    lines: list[str] = []
    while True:
        raw = await reader.readline()
        if not raw:
            raise ConnectionError("connection closed by peer")
        line = raw.decode(errors="replace").strip()
        if line:
            lines.append(line)
        if line.lower() == "ok":
            return "\n".join(lines)


async def _command(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cmd: str) -> str:
    writer.write(f"~{cmd}\r\n".encode())
    await writer.drain()
    return await asyncio.wait_for(_read_until_ok(reader), timeout=_COMMAND_TIMEOUT_SECONDS)


async def upload_gcode(
    ip_address: str,
    port: int,
    local_path: str,
    *,
    remote_name: str | None = None,
    password: str | None = None,  # unused — no auth on this transport
    use_https: bool = False,  # unused — raw TCP only
    start_print: bool = False,
) -> str:
    """Upload a g-code file to a FlashForge printer. Returns the remote filename."""
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)
    with open(local_path, "rb") as fh:
        body = fh.read()
    remote_path = f"0:/user/{name}"

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(ip_address, port), timeout=_CONNECT_TIMEOUT_SECONDS
    )
    try:
        await _command(reader, writer, "M601 S1")
        await _command(reader, writer, f"M28 {len(body)} {remote_path}")

        async def _send_body() -> None:
            writer.write(body)
            await writer.drain()

        await asyncio.wait_for(_send_body(), timeout=_UPLOAD_TIMEOUT_SECONDS)
        await _command(reader, writer, "M29")

        if start_print:
            await _command(reader, writer, f"M23 {remote_path}")
    finally:
        writer.close()

    logger.info("FlashForge upload OK: %s -> %s (%s:%s)", local_path, name, ip_address, port)
    return name
