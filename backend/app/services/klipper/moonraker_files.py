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


async def get_console(
    ip_address: str, port: int, *, count: int = 100, api_key: str | None = None
) -> dict:
    """Return Moonraker's recent g-code console log + the toolhead's homed axes.

    Powers the in-app Klipper console (realtime response log) and its
    "home first" hint. Read-only. Two cheap HTTP GETs against the Moonraker
    API port:

    * ``/server/gcode_store?count=N`` → the rolling console buffer. Each entry
      is ``{message, time, type}`` where type is ``command`` (echo of what was
      sent) or ``response`` (Klipper's reply, incl. ``!! error`` / ``// echo``).
    * ``/printer/objects/query?toolhead=homed_axes`` → e.g. ``"xyz"`` once homed,
      ``""`` before. Lets the UI show "not homed" and guide the user to Home
      instead of surfacing the raw "Must home axis first" error.

    Never raises for the homed lookup — if that query fails the console log is
    still returned with ``homed_axes: null``.
    """
    base = _http_base(ip_address, port)
    headers = _headers(api_key)
    store: list[dict] = []
    homed_axes: str | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{base}/server/gcode_store", params={"count": max(1, min(int(count), 1000))}, headers=headers
        )
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result") if isinstance(body, dict) else None
        if isinstance(result, dict):
            store = result.get("gcode_store", []) or []
        try:
            hr = await client.get(
                f"{base}/printer/objects/query", params={"toolhead": "homed_axes"}, headers=headers
            )
            hr.raise_for_status()
            hb = hr.json()
            hres = hb.get("result") if isinstance(hb, dict) else None
            th = ((hres or {}).get("status") or {}).get("toolhead") if isinstance(hres, dict) else None
            if isinstance(th, dict):
                homed_axes = str(th.get("homed_axes", "") or "")
        except Exception as exc:  # noqa: BLE001 — homed state is a nice-to-have
            logger.debug("[%s] homed_axes query failed: %s", ip_address, exc)
    return {"gcode_store": store, "homed_axes": homed_axes}


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
