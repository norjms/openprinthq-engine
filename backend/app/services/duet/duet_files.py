"""Upload g-code to a Duet printer (RRF ``rr_upload`` or DSF ``machine/file``).

Auto-detects the transport (dispatch has no persisted mode), then uploads to
``0:/gcodes/{name}`` — the SD path ``M32`` prints from.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0


def _base(scheme: str, ip: str, port: int) -> str:
    return f"{scheme}://{ip}:{port}/"


async def upload_gcode(
    ip_address: str,
    port: int,
    local_path: str,
    *,
    remote_name: str | None = None,
    password: str | None = None,
    use_https: bool = False,
    start_print: bool = False,
) -> str:
    """Upload a g-code file to a Duet. Returns the remote filename. Raises on failure."""
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)
    base = _base("https" if use_https else "http", ip_address, port)
    pw = password or "reprap"

    with open(local_path, "rb") as fh:
        body = fh.read()

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS, verify=False) as client:
        # Detect RRF vs DSF.
        session_key = None
        mode = "dsf"
        try:
            r = await client.get(f"{base}rr_connect?password={quote(pw)}")
            if r.status_code == 200:
                mode = "rrf"
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                sk = data.get("sessionKey")
                session_key = str(sk) if sk not in (None, "", "n/a") else None
        except Exception:  # noqa: BLE001
            mode = "dsf"

        headers = {"X-Session-Key": session_key} if session_key else {}
        if mode == "rrf":
            resp = await client.post(
                f"{base}rr_upload?name=0:/gcodes/{quote(name)}",
                content=body,
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            if start_print:
                start_cmd = 'M32 "0:/gcodes/' + name + '"'
                await client.get(f"{base}rr_gcode?gcode={quote(start_cmd)}", headers=headers)
        else:
            resp = await client.put(
                f"{base}machine/file/gcodes/{quote(name)}",
                content=body,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            if start_print:
                await client.post(f"{base}machine/code", content=f'M32 "0:/gcodes/{name}"')

    logger.info("Duet/%s upload OK: %s -> %s (%s)", mode, local_path, name, base)
    return name
