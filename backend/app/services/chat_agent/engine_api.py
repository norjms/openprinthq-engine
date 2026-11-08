"""Reads and actions for the chat agent, made as the linked engine user.

Every call goes through the engine's own HTTP API, in-process (an ASGI transport, no
network), with a short-lived token minted for the linked engine user. The route
dependencies, ownership filters, audit middleware and the per-printer scope guard
therefore apply to chat exactly as they apply to the web UI: nothing here re-implements
a permission rule. The token lives for two minutes, is never logged and never leaves
the process.

Printer visibility goes through ONE function, ``visible_printers`` (and its filter
``filter_visible_printers``). Every printer list, name lookup and per-printer read
in the chat agent starts from it. Per-printer scoping is applied twice: the engine API
answers as the linked user (the app-wide printer scope guard and the scoped printer
list), and ``filter_visible_printers`` narrows the list again with the user's scope
(``core.printer_scope.visible_printer_ids_for_user``), so a printer the user may not
see is never named in chat and never resolves for an action.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user import User

logger = logging.getLogger(__name__)

TOKEN_TTL = timedelta(minutes=2)
# Fields never carried into chat, even when the API returns them to this user.
_PRINTER_DROP = ("access_code",)


class EngineApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


async def filter_visible_printers(user: User, printers: list[dict], db: AsyncSession | None = None) -> list[dict]:
    """Narrow ``printers`` to the ones ``user``'s groups allow (per-printer scoping).

    The engine API already returns only the printers this user may see; this is the
    second filter (defence in depth) and the single place chat narrows the list.
    Admins, unscoped users and the auth-disabled case see every printer. Pass ``db`` to
    reuse an open session; otherwise a short one is opened for the lookup.
    """
    from backend.app.core.printer_scope import visible_printer_ids_for_user

    if db is None:
        from backend.app.core import database

        async with database.async_session() as session:
            allowed = await visible_printer_ids_for_user(session, user)
    else:
        allowed = await visible_printer_ids_for_user(db, user)
    if allowed is None:
        return printers
    return [p for p in printers if p.get("id") in allowed]


class EngineApi:
    """The engine HTTP API, called in-process as ``user``."""

    def __init__(self, user: User, actor_label: str, app=None):
        self.user = user
        self.actor_label = actor_label[:200]
        self._app = app

    def _get_app(self):
        if self._app is None:
            from backend.app.main import app

            self._app = app
        return self._app

    def _headers(self) -> dict:
        from backend.app.core.auth import create_access_token

        token = create_access_token({"sub": self.user.username}, expires_delta=TOKEN_TTL)
        return {"Authorization": f"Bearer {token}", "X-OPHQ-Actor": self.actor_label}

    async def request(self, method: str, path: str, **kwargs) -> Any:
        from backend.app.core.config import settings as app_settings

        transport = httpx.ASGITransport(app=self._get_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://chat-agent.internal") as client:
            resp = await client.request(method, app_settings.api_prefix + path, headers=self._headers(), **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:  # noqa: BLE001 - non-JSON error body
                detail = None
            if not isinstance(detail, str):
                detail = resp.reason_phrase or "error"
            raise EngineApiError(resp.status_code, detail)
        if not resp.content:
            return None
        return resp.json()

    async def get(self, path: str, **params) -> Any:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    # --- reads ---------------------------------------------------------------

    async def visible_printers(self) -> list[dict]:
        """The printers this user may see. All chat printer lookups start here."""
        rows = await self.get("/printers/")
        printers = [{k: v for k, v in p.items() if k not in _PRINTER_DROP} for p in rows or []]
        return await filter_visible_printers(self.user, printers)

    async def printer_status(self, printer_id: int) -> dict:
        return await self.get(f"/printers/{printer_id}/status")

    async def queue_items(self) -> list[dict]:
        return await self.get("/queue/", status="pending") or []

    async def recent_failures(self, limit: int = 10, printer_id: int | None = None) -> list[dict]:
        data = await self.get("/print-log/", status="failed", limit=limit, printer_id=printer_id)
        return (data or {}).get("items", [])

    async def active_alarms(self, printer_id: int | None = None) -> list[dict]:
        return await self.get("/alarms", state="active", printer_id=printer_id, limit=50) or []

    async def library_files(self) -> list[dict]:
        return await self.get("/library/files", include_root="false") or []

    # --- actions -------------------------------------------------------------

    async def queue_file(self, library_file_id: int, printer_id: int | None, target_model: str | None) -> dict:
        body: dict = {"library_file_id": library_file_id}
        if printer_id is not None:
            body["printer_id"] = printer_id
        if target_model:
            body["target_model"] = target_model
        return await self.request("POST", "/queue/", json=body)

    async def pause(self, printer_id: int) -> dict:
        return await self.request("POST", f"/printers/{printer_id}/print/pause")

    async def resume(self, printer_id: int) -> dict:
        return await self.request("POST", f"/printers/{printer_id}/print/resume")

    async def cancel(self, printer_id: int) -> dict:
        return await self.request("POST", f"/printers/{printer_id}/print/stop")

    async def out_of_service(self, printer_id: int, reason: str | None) -> dict:
        return await self.request("PUT", f"/printers/{printer_id}/out-of-service", json={"reason": reason})

    async def back_in_service(self, printer_id: int) -> dict:
        return await self.request("DELETE", f"/printers/{printer_id}/out-of-service")
