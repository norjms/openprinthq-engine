"""Upload g-code to an OctoPrint / PrusaLink printer.

OctoPrint: multipart ``POST /api/files/local``. PrusaLink: raw
``PUT /api/v1/files/{storage}/{name}``. Both return the remote filename to pass
to the client's ``start_print``.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0


def _base(scheme: str, ip: str, port: int) -> str:
    return f"{scheme}://{ip}:{port}"


def _headers(api_key: str | None) -> dict[str, str]:
    return {"X-Api-Key": api_key} if api_key else {}


async def upload_gcode(
    ip_address: str,
    port: int,
    local_path: str,
    *,
    remote_name: str | None = None,
    api_key: str | None = None,
    flavor: str = "octoprint",
    use_https: bool = False,
    start_print: bool = False,
    storage: str = "local",
) -> str:
    """Upload a local g-code file. Returns the remote filename. Raises on failure."""
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)
    scheme = "https" if use_https else "http"
    base = _base(scheme, ip_address, port)

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS, verify=False) as client:
        if flavor == "prusalink":
            with open(local_path, "rb") as fh:
                headers = {
                    **_headers(api_key),
                    "Content-Type": "application/octet-stream",
                    "Print-After-Upload": "1" if start_print else "0",
                    "Overwrite": "1",
                }
                resp = await client.put(f"{base}/api/v1/files/{storage}/{name}", content=fh.read(), headers=headers)
            resp.raise_for_status()
        else:
            with open(local_path, "rb") as fh:
                files = {"file": (name, fh, "application/octet-stream")}
                data = {"select": "true" if start_print else "false", "print": "true" if start_print else "false"}
                resp = await client.post(
                    f"{base}/api/files/local", headers=_headers(api_key), files=files, data=data
                )
            resp.raise_for_status()

    logger.info("OctoPrint/%s upload OK: %s -> %s (%s)", flavor, local_path, name, base)
    return name


async def get_webcam_snapshot_url(ip_address: str, port: int, *, api_key: str | None = None) -> str | None:
    """Best-effort: read OctoPrint's configured webcam snapshot URL."""
    scheme = "http"
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            r = await client.get(f"{_base(scheme, ip_address, port)}/api/settings", headers=_headers(api_key))
            r.raise_for_status()
            webcam = (r.json() or {}).get("webcam") or {}
            return webcam.get("streamUrl") or webcam.get("snapshotUrl") or None
    except Exception:  # noqa: BLE001
        return None
