"""Upload g-code to Obico (self-hosted print-farm relay).

Matches OrcaSlicer's ``Obico::upload`` exactly: multipart POST to
``{host}/api/v1/g_code_files/`` with ``print``/``path``/``printer_id``/
``filename``/``file`` fields, ``Authorization: Bearer <api_key>`` auth.
``printer_id`` is the target printer's ID *within the Obico account* (an
Obico concept, unrelated to Bambuddy's own printer id) — reused from
``Printer.model`` for this connection type.
"""

from __future__ import annotations

import logging
import os

import httpx

from backend.app.services.obico.obico_client import normalize_host

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 300.0


async def upload_gcode(
    host: str,
    api_key: str,
    obico_printer_id: str,
    local_path: str,
    *,
    remote_name: str | None = None,
    start_print: bool = False,
) -> str:
    """Upload a g-code file to Obico. Returns the remote filename. Raises on failure."""
    if not api_key:
        raise ValueError("Obico requires an API key")
    if not obico_printer_id:
        raise ValueError("Obico requires the target printer's Obico printer ID")
    if not os.path.isfile(local_path):
        raise FileNotFoundError(local_path)
    name = remote_name or os.path.basename(local_path)
    base = normalize_host(host)

    with open(local_path, "rb") as fh:
        body = fh.read()

    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{base}/api/v1/g_code_files/",
            headers={"Authorization": f"Bearer {api_key}"},
            data={
                "print": "true" if start_print else "false",
                "path": "",
                "printer_id": obico_printer_id,
                "filename": name,
            },
            files={"file": (name, body, "application/octet-stream")},
        )
        resp.raise_for_status()

    logger.info("Obico upload OK: %s -> %s (%s, printer_id=%s)", local_path, name, base, obico_printer_id)
    return name
