"""Chat agent settings and account-link API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.chat_agent import ChatIdentityLink
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services.chat_agent.config import SECRET_SETTING_KEYS

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_connect():
    with patch("backend.app.api.routes.chat_agent.chat_agent_manager.apply", new_callable=AsyncMock) as apply:
        yield apply


async def _auth(async_client):
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "cadmin", "admin_password": "AdminPass1!"},
    )
    r = await async_client.post("/api/v1/auth/login", json={"username": "cadmin", "password": "AdminPass1!"})
    admin_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    groups = {g["name"]: g["id"] for g in (await async_client.get("/api/v1/groups/", headers=admin_h)).json()}
    await async_client.post(
        "/api/v1/users/",
        headers=admin_h,
        json={"username": "vw", "password": "Userpass1!xx", "group_ids": [groups["Viewers"]]},
    )
    r = await async_client.post("/api/v1/auth/login", json={"username": "vw", "password": "Userpass1!xx"})
    return admin_h, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_config_defaults_off(async_client):
    r = await async_client.get("/api/v1/chat-agent/config")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["slack_bot_token_set"] is False
    assert body["confirm_timeout_seconds"] == 120
    assert body["status"]["slack"] == {"running": False, "error": None}


async def test_tokens_write_only_and_encrypted(async_client, db_session, no_connect):
    r = await async_client.put(
        "/api/v1/chat-agent/config",
        json={
            "enabled": True,
            "slack_enabled": True,
            "slack_bot_token": "xoxb-very-secret",
            "slack_app_token": "xapp-very-secret",
            "slack_allowed_channels": ["C1", " C2 ", "C1"],
            "confirm_timeout_seconds": 300,
        },
    )
    assert r.status_code == 200, r.text
    assert "very-secret" not in r.text
    body = r.json()
    assert body["slack_bot_token_set"] and body["slack_app_token_set"]
    assert body["slack_allowed_channels"] == ["C1", "C2"]
    assert body["confirm_timeout_seconds"] == 300
    applied = no_connect.await_args.args[0]
    assert applied.slack_ready and applied.slack_bot_token == "xoxb-very-secret"
    stored = (
        await db_session.execute(select(Settings).where(Settings.key == "chat_agent_slack_bot_token"))
    ).scalar_one()
    assert stored.value.startswith("fernet:") and "very-secret" not in stored.value
    assert "chat_agent_slack_bot_token" in SECRET_SETTING_KEYS

    # Leaving a token out keeps it; "" clears it.
    r = await async_client.put("/api/v1/chat-agent/config", json={"allow_dms": True})
    assert r.json()["slack_bot_token_set"] is True and r.json()["allow_dms"] is True
    r = await async_client.put("/api/v1/chat-agent/config", json={"slack_bot_token": ""})
    assert r.json()["slack_bot_token_set"] is False


async def test_env_token_wins(async_client, monkeypatch):
    monkeypatch.setenv("OPHQ_CHAT_DISCORD_BOT_TOKEN", "env-token")
    body = (await async_client.get("/api/v1/chat-agent/config")).json()
    assert body["discord_bot_token_set"] and body["discord_bot_token_from_env"]


async def test_timeout_bounds(async_client):
    r = await async_client.put("/api/v1/chat-agent/config", json={"confirm_timeout_seconds": 5})
    assert r.status_code == 422


async def test_config_needs_settings_permissions(async_client):
    _admin_h, viewer_h = await _auth(async_client)
    assert (
        await async_client.put("/api/v1/chat-agent/config", headers=viewer_h, json={"enabled": True})
    ).status_code == 403
    assert (await async_client.get("/api/v1/chat-agent/config")).status_code == 401


async def test_link_codes_and_links(async_client, db_session):
    admin_h, viewer_h = await _auth(async_client)
    users = {u.username: u for u in (await db_session.execute(select(User))).scalars().all()}

    r = await async_client.post("/api/v1/chat-agent/link-codes", headers=viewer_h, json={})
    assert r.status_code == 200, r.text
    code = r.json()
    assert code["username"] == "vw" and len(code["code"]) == 9

    # A viewer cannot make a code for someone else; an admin can.
    r = await async_client.post("/api/v1/chat-agent/link-codes", headers=viewer_h, json={"user_id": users["cadmin"].id})
    assert r.status_code == 403
    r = await async_client.post("/api/v1/chat-agent/link-codes", headers=admin_h, json={"user_id": users["vw"].id})
    assert r.status_code == 200 and r.json()["username"] == "vw"

    db_session.add(ChatIdentityLink(platform="slack", external_user_id="U1", user_id=users["vw"].id))
    db_session.add(ChatIdentityLink(platform="discord", external_user_id="55", user_id=users["cadmin"].id))
    await db_session.commit()

    mine = (await async_client.get("/api/v1/chat-agent/links", headers=viewer_h)).json()
    assert [(lk["platform"], lk["username"]) for lk in mine] == [("slack", "vw")]
    assert (await async_client.get("/api/v1/chat-agent/links?all=true", headers=viewer_h)).status_code == 403
    everyone = (await async_client.get("/api/v1/chat-agent/links?all=true", headers=admin_h)).json()
    assert len(everyone) == 2

    admins_link = next(lk for lk in everyone if lk["platform"] == "discord")
    assert (
        await async_client.delete(f"/api/v1/chat-agent/links/{admins_link['id']}", headers=viewer_h)
    ).status_code == 404
    assert (await async_client.delete(f"/api/v1/chat-agent/links/{mine[0]['id']}", headers=viewer_h)).status_code == 200
    assert (
        await async_client.delete(f"/api/v1/chat-agent/links/{admins_link['id']}", headers=admin_h)
    ).status_code == 200


async def test_sign_in_off_needs_explicit_user(async_client, db_session):
    r = await async_client.post("/api/v1/chat-agent/link-codes", json={})
    assert r.status_code == 400
    u = User(username="solo", password_hash="x", role="admin", is_active=True)
    db_session.add(u)
    await db_session.commit()
    r = await async_client.post("/api/v1/chat-agent/link-codes", json={"user_id": u.id})
    assert r.status_code == 200 and r.json()["username"] == "solo"


async def test_backup_leaves_chat_tokens_out(async_client, db_session):
    from backend.app.services.github_backup import GitHubBackupService

    db_session.add(Settings(key="chat_agent_discord_bot_token", value="fernet:abc"))
    db_session.add(Settings(key="chat_agent_enabled", value="true"))
    await db_session.commit()
    files: dict = {}
    await GitHubBackupService()._collect_settings(db_session, files)
    saved = files["settings/app_settings.json"]["settings"]
    assert "chat_agent_discord_bot_token" not in saved and saved["chat_agent_enabled"] == "true"
