"""Chat agent and per-printer scoping: a linked user only sees and acts on the printers
their groups allow, in the agent's own printer list (``filter_visible_printers``) and
again in the engine API it calls as that user.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.services.chat_agent import engine_api
from backend.app.services.chat_agent.agent import ChatAgent, IncomingMessage, create_link_code
from backend.app.services.chat_agent.config import ChatAgentConfig
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


def _cfg() -> ChatAgentConfig:
    return ChatAgentConfig(
        enabled=True,
        slack_enabled=True,
        slack_allowed_channels=["C1"],
        slack_allowed_workspaces=["T1"],
        confirm_timeout_seconds=120,
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
    )


def msg(text: str, user: str = "U1") -> IncomingMessage:
    return IncomingMessage(platform="slack", user_id=user, channel_id="C1", text=text, team_id="T1", is_dm=False)


@pytest.fixture
async def scoped_farm(async_client, db_session, printer_factory):
    """Sign-in on; an operator scoped to one printer, an unscoped operator; two printers."""
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "scopeadmin", "admin_password": "AdminPass1!"},
    )
    login = await async_client.post("/api/v1/auth/login", json={"username": "scopeadmin", "password": "AdminPass1!"})
    admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    p_in = await printer_factory(name="Voron 2.4", model="P1S")
    p_out = await printer_factory(name="X1C-1")
    groups = (await async_client.get("/api/v1/groups/", headers=admin_h)).json()
    operators = next(g for g in groups if g["name"] == "Operators")
    r = await async_client.post(
        "/api/v1/groups/",
        headers=admin_h,
        json={"name": "Voron crew", "permissions": operators["permissions"], "scope_printer_ids": [p_in.id]},
    )
    assert r.status_code in (200, 201), r.text
    for name, gid in (("scop", r.json()["id"]), ("wide", operators["id"])):
        r = await async_client.post(
            "/api/v1/users/",
            headers=admin_h,
            json={"username": name, "password": "Userpass1!xx", "group_ids": [gid]},
        )
        assert r.status_code in (200, 201), r.text
    users = {u.username: u for u in (await db_session.execute(select(User))).scalars().all()}
    return {"users": users, "in_id": p_in.id, "out_id": p_out.id}


async def _link(agent: ChatAgent, db_session, user: User, chat_user: str = "U1") -> None:
    code, _ = await create_link_code(db_session, user.id, "test")
    replies = await agent.handle_message(msg(f"link {code}", user=chat_user))
    assert replies and replies[0].text.startswith("Linked"), replies


@pytest.fixture
def client_mock():
    client = MagicMock()
    client.pause_print.return_value = True
    client.resume_print.return_value = True
    with patch.object(printer_manager, "get_client", return_value=client):
        yield client


class TestChatAgentPrinterScope:
    async def test_filter_applies_the_users_scope(self, scoped_farm):
        printers = [{"id": scoped_farm["in_id"]}, {"id": scoped_farm["out_id"]}]
        scoped = await engine_api.filter_visible_printers(scoped_farm["users"]["scop"], printers)
        assert [p["id"] for p in scoped] == [scoped_farm["in_id"]]
        wide = await engine_api.filter_visible_printers(scoped_farm["users"]["wide"], printers)
        assert wide == printers
        admin = await engine_api.filter_visible_printers(scoped_farm["users"]["scopeadmin"], printers)
        assert admin == printers

    async def test_scoped_status_shows_only_in_scope_printers(self, scoped_farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, scoped_farm["users"]["scop"])
        status = (await agent.handle_message(msg("status")))[0].text
        assert "Voron 2.4" in status and "X1C-1" not in status
        reply = (await agent.handle_message(msg("how is X1C-1 doing?")))[0].text
        assert "X1C-1:" not in reply

    async def test_scoped_user_cannot_pause_out_of_scope_printer(self, scoped_farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, scoped_farm["users"]["scop"])
        reply = (await agent.handle_message(msg("pause X1C-1")))[0].text
        assert "do not see a printer" in reply and "X1C-1" not in reply.split(".", 1)[1]
        client_mock.pause_print.assert_not_called()

    async def test_scoped_user_can_pause_in_scope_printer(self, scoped_farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, scoped_farm["users"]["scop"])
        reply = (await agent.handle_message(msg("pause Voron 2.4")))[0].text
        assert reply == "Paused the print on Voron 2.4."
        client_mock.pause_print.assert_called_once()

    async def test_unscoped_user_sees_every_printer(self, scoped_farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, scoped_farm["users"]["wide"], chat_user="U2")
        status = (await agent.handle_message(msg("status", user="U2")))[0].text
        assert "Voron 2.4" in status and "X1C-1" in status

    async def test_engine_api_still_refuses_when_the_chat_filter_is_bypassed(
        self, scoped_farm, db_session, client_mock, monkeypatch
    ):
        """Second layer: the in-process API calls run as the linked user, so the app-wide
        printer scope guard answers 404 even if the chat agent's own filter let a printer through."""

        async def no_filter(user, printers, db=None):
            return printers

        monkeypatch.setattr(engine_api, "filter_visible_printers", no_filter)
        user = scoped_farm["users"]["scop"]
        api = engine_api.EngineApi(user, "chat:test")
        listed = {p["id"] for p in await api.visible_printers()}
        assert scoped_farm["out_id"] not in listed
        with pytest.raises(engine_api.EngineApiError) as e:
            await api.printer_status(scoped_farm["out_id"])
        assert e.value.status == 404
        with pytest.raises(engine_api.EngineApiError) as e:
            await api.pause(scoped_farm["out_id"])
        assert e.value.status == 404
        client_mock.pause_print.assert_not_called()
        assert (await api.printer_status(scoped_farm["in_id"])) is not None
