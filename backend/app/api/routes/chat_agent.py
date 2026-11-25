"""Chat agent settings and account links (OpenPrintHQ farm feature).

See services/chat_agent/ and docs/chat-agent.md.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled, is_auth_enabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.chat_agent import ChatIdentityLink
from backend.app.models.user import User
from backend.app.services.chat_agent.agent import create_link_code
from backend.app.services.chat_agent.config import (
    CONFIRM_TIMEOUT_MAX,
    CONFIRM_TIMEOUT_MIN,
    load_config,
    save_config,
)
from backend.app.services.chat_agent.manager import chat_agent_manager

router = APIRouter(prefix="/chat-agent", tags=["chat-agent"])


class PlatformStatus(BaseModel):
    running: bool
    error: str | None = None


class ChatAgentConfigOut(BaseModel):
    enabled: bool
    slack_enabled: bool
    discord_enabled: bool
    allow_dms: bool
    llm_enabled: bool
    slack_allowed_channels: list[str]
    slack_allowed_workspaces: list[str]
    discord_allowed_channels: list[str]
    discord_allowed_guilds: list[str]
    llm_base_url: str
    llm_model: str
    confirm_timeout_seconds: int
    slack_bot_token_set: bool
    slack_bot_token_from_env: bool
    slack_app_token_set: bool
    slack_app_token_from_env: bool
    discord_bot_token_set: bool
    discord_bot_token_from_env: bool
    llm_api_key_set: bool
    llm_api_key_from_env: bool
    status: dict[str, PlatformStatus]


class ChatAgentConfigUpdate(BaseModel):
    """Partial update. For tokens: a value sets it, "" clears it, leaving it out keeps it."""

    enabled: bool | None = None
    slack_enabled: bool | None = None
    discord_enabled: bool | None = None
    allow_dms: bool | None = None
    llm_enabled: bool | None = None
    slack_allowed_channels: list[str] | None = Field(default=None, max_length=100)
    slack_allowed_workspaces: list[str] | None = Field(default=None, max_length=100)
    discord_allowed_channels: list[str] | None = Field(default=None, max_length=100)
    discord_allowed_guilds: list[str] | None = Field(default=None, max_length=100)
    llm_base_url: str | None = Field(default=None, max_length=500)
    llm_model: str | None = Field(default=None, max_length=200)
    confirm_timeout_seconds: int | None = Field(default=None, ge=CONFIRM_TIMEOUT_MIN, le=CONFIRM_TIMEOUT_MAX)
    slack_bot_token: str | None = Field(default=None, max_length=500)
    slack_app_token: str | None = Field(default=None, max_length=500)
    discord_bot_token: str | None = Field(default=None, max_length=500)
    llm_api_key: str | None = Field(default=None, max_length=500)


class ChatLinkOut(BaseModel):
    id: int
    platform: str
    external_user_id: str
    external_team_id: str | None = None
    display_name: str | None = None
    user_id: int
    username: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None


class LinkCodeRequest(BaseModel):
    # Make the code for another engine user (needs users:update). Default: yourself.
    user_id: int | None = None


class LinkCodeOut(BaseModel):
    code: str
    user_id: int
    username: str
    expires_at: datetime


async def _config_out(db: AsyncSession) -> ChatAgentConfigOut:
    cfg = await load_config(db)
    return ChatAgentConfigOut(**cfg.public_dict(), status=chat_agent_manager.status())


@router.get("/config", response_model=ChatAgentConfigOut)
async def get_chat_agent_config(
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
    db: AsyncSession = Depends(get_db),
):
    """Chat agent settings. Token values are never returned, only whether each is set."""
    return await _config_out(db)


@router.put("/config", response_model=ChatAgentConfigOut)
async def update_chat_agent_config(
    body: ChatAgentConfigUpdate,
    _=RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Save chat agent settings and restart the Slack / Discord connections to match."""
    await save_config(db, body.model_dump(exclude_unset=True))
    await chat_agent_manager.apply(await load_config(db))
    return await _config_out(db)


async def _caller(user: User | None, db: AsyncSession) -> tuple[User | None, bool]:
    """(signed-in user or None, may manage other users' links)."""
    if user is not None:
        return user, user.has_permission(Permission.USERS_UPDATE)
    if await is_auth_enabled(db):
        # Sign-in is on but there is no user: an API key. Links belong to people.
        raise HTTPException(403, "Chat account links are managed by signed-in users")
    return None, True  # sign-in off: same as every other route, no per-user limits


def _link_out(link: ChatIdentityLink, usernames: dict[int, str]) -> ChatLinkOut:
    return ChatLinkOut(
        id=link.id,
        platform=link.platform,
        external_user_id=link.external_user_id,
        external_team_id=link.external_team_id,
        display_name=link.display_name,
        user_id=link.user_id,
        username=usernames.get(link.user_id),
        created_at=link.created_at,
        last_used_at=link.last_used_at,
    )


@router.get("/links", response_model=list[ChatLinkOut])
async def list_chat_links(
    all_users: bool = Query(False, alias="all"),
    user: User | None = RequirePermissionIfAuthEnabled(),
    db: AsyncSession = Depends(get_db),
):
    """Chat accounts linked to your engine account; ``all=true`` lists everyone's (users:read)."""
    me, _admin = await _caller(user, db)
    q = select(ChatIdentityLink).order_by(ChatIdentityLink.created_at.desc(), ChatIdentityLink.id.desc())
    if all_users:
        if me is not None and not me.has_permission(Permission.USERS_READ):
            raise HTTPException(403, "Missing permission: users:read")
    elif me is not None:
        q = q.where(ChatIdentityLink.user_id == me.id)
    links = (await db.execute(q)).scalars().all()
    ids = {lk.user_id for lk in links}
    usernames = {}
    if ids:
        usernames = dict((await db.execute(select(User.id, User.username).where(User.id.in_(ids)))).all())
    return [_link_out(lk, usernames) for lk in links]


@router.delete("/links/{link_id}")
async def delete_chat_link(
    link_id: int,
    user: User | None = RequirePermissionIfAuthEnabled(),
    db: AsyncSession = Depends(get_db),
):
    """Unlink a chat account. Your own, or anyone's with users:update."""
    me, admin = await _caller(user, db)
    link = (await db.execute(select(ChatIdentityLink).where(ChatIdentityLink.id == link_id))).scalar_one_or_none()
    if link is None or (not admin and (me is None or link.user_id != me.id)):
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await db.commit()
    return {"success": True}


@router.post("/link-codes", response_model=LinkCodeOut)
async def create_chat_link_code(
    body: LinkCodeRequest,
    user: User | None = RequirePermissionIfAuthEnabled(),
    db: AsyncSession = Depends(get_db),
):
    """A single-use code (10 minutes) to send to the bot: ``link <code>``."""
    me, admin = await _caller(user, db)
    target_id = body.user_id if body.user_id is not None else (me.id if me else None)
    if target_id is None:
        raise HTTPException(400, "Choose the engine user to link (sign-in is off)")
    if me is not None and target_id != me.id and not admin:
        raise HTTPException(403, "Missing permission: users:update")
    target = (await db.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
    if target is None or not target.is_active:
        raise HTTPException(404, "User not found")
    code, expires = await create_link_code(db, target.id, me.username if me else None)
    return LinkCodeOut(code=code, user_id=target.id, username=target.username, expires_at=expires)
