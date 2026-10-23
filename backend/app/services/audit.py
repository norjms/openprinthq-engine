"""Audit log: every change made through the API (OpenPrintHQ farm feature).

A middleware records each non-GET request under /api/ after it has been
answered: when, who, from where, the method, the route template and the path,
the status code and how long it took. Request and response bodies are never
stored, so secrets sent to the API (access codes, passwords, keys) cannot end up
in the log; neither are query strings (tokens are sometimes passed there).

Who:
- a JWT (auth enabled): the user name in the token (not re-validated here; the
  auth middleware and route dependencies already did that);
- an API key (X-API-Key or "Bearer bb_..."): "api_key:" plus the key's prefix;
- behind the OpenPrintHQ control plane, where tenant engines run with sign-in
  off, the control plane names the person or integration key in X-OPHQ-Actor.
  Recorded as actor_kind "gateway"; it is a label from the gateway, not a proof.

Each write is one small insert after the response is ready; a failed write is
logged and never fails the request. Entries older than ``audit_retention_days`` (default 90) are purged
once a day.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from backend.app.core import database
from backend.app.models.audit_log import AuditLogEntry
from backend.app.models.settings import Settings

logger = logging.getLogger(__name__)

SKIP_METHODS = {"GET", "HEAD", "OPTIONS"}
# High-frequency, state-free calls that would drown the log.
SKIP_PREFIXES = ("/api/v1/auth/refresh", "/api/v1/metrics")


def actor_of(headers) -> tuple[str | None, str]:
    auth = headers.get("authorization") or ""
    key = headers.get("x-api-key")
    if key or auth.startswith("Bearer bb_"):
        raw = key or auth[len("Bearer ") :]
        return f"api_key:{raw[:8]}", "api_key"
    if auth.startswith("Bearer "):
        try:
            import jwt

            payload = jwt.decode(auth[len("Bearer ") :], options={"verify_signature": False})
            sub = payload.get("sub")
            if sub:
                return str(sub)[:200], "user"
        except Exception:  # noqa: BLE001 - an unreadable token is just anonymous here
            pass
    gw = headers.get("x-ophq-actor")
    if gw:
        return gw[:200], "gateway"
    return None, "anonymous"


def should_log(method: str, path: str) -> bool:
    return method.upper() not in SKIP_METHODS and path.startswith("/api/") and not path.startswith(SKIP_PREFIXES)


async def write(entry: dict) -> None:
    try:
        async with database.async_session() as db:
            db.add(AuditLogEntry(**entry))
            await db.commit()
    except Exception as e:  # noqa: BLE001 - the audit log must never break a request
        logger.warning("audit log write failed: %s", e)


def record_request(request, status: int, started: float) -> dict | None:
    """Build the entry for a finished request, or None when it is not logged."""
    path = request.url.path
    if not should_log(request.method, path):
        return None
    actor, kind = actor_of(request.headers)
    route = getattr(request.scope.get("route"), "path", None)
    client = request.client.host if request.client else None
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or client
    return {
        "actor": actor,
        "actor_kind": kind,
        "ip": (ip or None) and ip[:64],
        "method": request.method.upper()[:10],
        "route": route[:300] if route else None,
        "path": path[:500],
        "status": int(status),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


async def retention_days(db) -> int:
    row = (await db.execute(select(Settings).where(Settings.key == "audit_retention_days"))).scalar_one_or_none()
    try:
        return max(1, int(row.value)) if row else 90
    except ValueError:
        return 90


async def purge_old() -> int:
    async with database.async_session() as db:
        days = await retention_days(db)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        res = await db.execute(delete(AuditLogEntry).where(AuditLogEntry.ts < cutoff))
        await db.commit()
        return res.rowcount or 0


async def purge_loop() -> None:
    while True:
        try:
            n = await purge_old()
            if n:
                logger.info("audit log: purged %d old entries", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("audit log purge failed: %s", e)
        await asyncio.sleep(24 * 3600)
