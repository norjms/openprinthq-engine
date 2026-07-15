"""Moonraker file operations — upload/list/delete gcode on a Klipper printer.

Klipper prints files from Moonraker's ``gcodes`` root. Bambuddy uploads a
``.gcode`` from its Library here, then tells Klipper to print it by name.
Unlike Bambu (FTPS + 3MF), this is a plain HTTP multipart upload.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0


def _http_base(ip_address: str, port: int) -> str:
    return f"http://{ip_address}:{port}"


def _headers(api_key: str | None) -> dict[str, str]:
    return {"X-Api-Key": api_key} if api_key else {}


async def upload_gcode(
    ip_address: str,
    port: int,
    local_path: str,
    *,
    remote_name: str | None = None,
    api_key: str | None = None,
    start_print: bool = False,
) -> str:
    """Upload a local gcode file to Moonraker's gcode store.

    Returns the remote filename Moonraker stored it under (use it with
    ``printer.print.start``). Raises on failure.
    """
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
        with open(local_path, "rb") as fh:
            files = {"file": (name, fh, "application/octet-stream")}
            data = {"root": "gcodes", "print": "true" if start_print else "false"}
            resp = await client.post(
                f"{_http_base(ip_address, port)}/server/files/upload",
                headers=_headers(api_key),
                files=files,
                data=data,
            )
        resp.raise_for_status()
        body = resp.json()
    # Response shape: {"item": {"path": "<name>", "root": "gcodes"}, ...}
    item = body.get("item") if isinstance(body, dict) else None
    stored = (item or {}).get("path") if isinstance(item, dict) else None
    stored = stored or name
    logger.info("Moonraker upload OK: %s -> %s:%s/gcodes/%s", local_path, ip_address, port, stored)
    return stored


async def list_gcodes(ip_address: str, port: int, *, api_key: str | None = None) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{_http_base(ip_address, port)}/server/files/list",
            headers=_headers(api_key),
            params={"root": "gcodes"},
        )
        resp.raise_for_status()
        body = resp.json()
    return body.get("result", []) if isinstance(body, dict) else []


async def get_webcams(ip_address: str, port: int, *, api_key: str | None = None) -> list[dict]:
    """Return Moonraker's configured webcams (``/server/webcams/list``).

    Each entry has at least ``name``, ``stream_url``, ``snapshot_url``. URLs may
    be relative to the host root (served by the host's nginx, not Moonraker's
    port), so callers should absolutise relative paths against the host.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{_http_base(ip_address, port)}/server/webcams/list",
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        body = resp.json()
    result = body.get("result") if isinstance(body, dict) else None
    return (result or {}).get("webcams", []) if isinstance(result, dict) else []


def absolutise_webcam_url(ip_address: str, url: str | None) -> str | None:
    """Make a Moonraker webcam URL absolute.

    Moonraker reports webcam URLs relative to the host root (e.g.
    ``/webcam/?action=stream``), served by the host's web server on port 80 —
    NOT Moonraker's API port. Leave already-absolute URLs untouched.
    """
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return f"http://{ip_address}{url}"


async def delete_gcode(ip_address: str, port: int, remote_name: str, *, api_key: str | None = None) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{_http_base(ip_address, port)}/server/files/gcodes/{remote_name}",
            headers=_headers(api_key),
        )
        resp.raise_for_status()
