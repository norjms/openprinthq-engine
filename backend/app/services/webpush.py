"""Web push: notifications straight to browsers and installed web apps.

A browser subscribes once (POST /api/v1/push/subscribe with the subscription
the Push API gives it). A notification provider of type ``webpush`` then sends
every event it is enabled for to all subscribed browsers, through the
browser vendors' push services (no third-party account needed).

The VAPID key pair that identifies this server to the push services is made on
first use and kept in settings (``webpush_vapid_private_pem``,
``webpush_vapid_public``); support bundles redact it. A subscription the push
service reports as gone (404/410) is deleted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.core import database
from backend.app.models.push_subscription import PushSubscription
from backend.app.models.settings import Settings

logger = logging.getLogger(__name__)

PRIVATE_KEY = "webpush_vapid_private_pem"
PUBLIC_KEY = "webpush_vapid_public"
CONTACT = "https://github.com/norjms/openprinthq"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _generate() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, _b64url(pub)


async def vapid_keys(db) -> tuple[str, str]:
    rows = {
        r.key: r
        for r in (await db.execute(select(Settings).where(Settings.key.in_([PRIVATE_KEY, PUBLIC_KEY])))).scalars()
    }
    if PRIVATE_KEY in rows and PUBLIC_KEY in rows:
        return rows[PRIVATE_KEY].value, rows[PUBLIC_KEY].value
    pem, pub = _generate()
    for k, v in ((PRIVATE_KEY, pem), (PUBLIC_KEY, pub)):
        if k in rows:
            rows[k].value = v
        else:
            db.add(Settings(key=k, value=v))
    await db.commit()
    return pem, pub


def _send_one(sub: PushSubscription, payload: str, pem: str) -> int | None:
    """Returns None on success, else the HTTP status from the push service (0 if none)."""
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=payload,
            vapid_private_key=Vapid02.from_pem(pem.encode()),
            vapid_claims={"sub": CONTACT},
            timeout=10,
            ttl=3600,
        )
        return None
    except WebPushException as e:
        return getattr(e.response, "status_code", 0) or 0


async def send_all(title: str, message: str, url: str | None = None, db=None) -> tuple[bool, str]:
    async def _run(session):
        pem, _pub = await vapid_keys(session)
        subs = (await session.execute(select(PushSubscription))).scalars().all()
        if not subs:
            return False, "No browser is subscribed to push notifications"
        payload = json.dumps({"title": title, "body": message, "url": url or "/"})
        ok, gone, failed = 0, 0, 0
        for sub in subs:
            status = await asyncio.to_thread(_send_one, sub, payload, pem)
            if status is None:
                ok += 1
                sub.last_success_at = datetime.now(timezone.utc).replace(tzinfo=None)
            elif status in (404, 410):
                gone += 1
                await session.delete(sub)
            else:
                failed += 1
                logger.warning("web push to %s... failed: HTTP %s", sub.endpoint[:40], status)
        await session.commit()
        msg = (
            f"Sent to {ok} browser(s)"
            + (f", {gone} expired removed" if gone else "")
            + (f", {failed} failed" if failed else "")
        )
        return ok > 0, msg

    if db is not None:
        return await _run(db)
    async with database.async_session() as session:
        return await _run(session)
