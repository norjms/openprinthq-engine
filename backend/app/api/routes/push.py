"""Web push subscriptions (OpenPrintHQ farm feature). See services/webpush.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import require_permission_if_auth_enabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.push_subscription import PushSubscription
from backend.app.models.user import User
from backend.app.services import webpush

router = APIRouter(prefix="/push", tags=["notifications"])

_read = require_permission_if_auth_enabled(Permission.PRINTERS_READ)


class Keys(BaseModel):
    p256dh: str = Field(..., min_length=10, max_length=500)
    auth: str = Field(..., min_length=8, max_length=200)


class Subscription(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=1000)
    keys: Keys


class Unsubscribe(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=1000)


@router.get("/vapid-public-key")
async def vapid_public_key(db: AsyncSession = Depends(get_db), _: User | None = Depends(_read)):
    """The key a browser passes to PushManager.subscribe as applicationServerKey."""
    _pem, pub = await webpush.vapid_keys(db)
    return {"public_key": pub}


@router.post("/subscribe", status_code=201)
async def subscribe(body: Subscription, db: AsyncSession = Depends(get_db), user: User | None = Depends(_read)):
    if not body.endpoint.startswith("https://"):
        raise HTTPException(400, "Push endpoints are https URLs")
    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    ).scalar_one_or_none()
    if row is None:
        row = PushSubscription(endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth)
        db.add(row)
    else:
        row.p256dh, row.auth = body.keys.p256dh, body.keys.auth
    row.user_id = user.id if user is not None else None
    await db.commit()
    return {"success": True}


@router.delete("/subscribe")
async def unsubscribe(body: Unsubscribe, db: AsyncSession = Depends(get_db), _: User | None = Depends(_read)):
    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"success": True}
