"""Chat agent end to end: linking, channel restriction, permissions, confirmations, audit.

The agent calls the real engine API in-process as the linked user, so these tests
exercise the same route dependencies the web UI uses. Printer clients are mocked.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.audit_log import AuditLogEntry
from backend.app.models.chat_agent import ChatIdentityLink, ChatLinkCode
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import audit
from backend.app.services.chat_agent import engine_api
from backend.app.services.chat_agent.agent import LINK_TEXT, ChatAgent, IncomingMessage, create_link_code
from backend.app.services.chat_agent.config import ChatAgentConfig
from backend.app.services.chat_agent.intents import Intent, ParsedIntent
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _cfg(**kw) -> ChatAgentConfig:
    base = {
        "enabled": True,
        "slack_enabled": True,
        "discord_enabled": True,
        "slack_allowed_channels": ["C1"],
        "slack_allowed_workspaces": ["T1"],
        "discord_allowed_channels": ["900"],
        "confirm_timeout_seconds": 120,
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
    }
    base.update(kw)
    return ChatAgentConfig(**base)


def msg(text, user="U1", channel="C1", team="T1", platform="slack", is_dm=False):
    return IncomingMessage(platform=platform, user_id=user, channel_id=channel, text=text, team_id=team, is_dm=is_dm)


@pytest.fixture
async def farm(async_client, db_session, printer_factory, monkeypatch):
    """Sign-in on; an operator and a viewer; two printers; a library file."""
    monkeypatch.setattr(audit, "ENABLED", True)
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "chatadmin", "admin_password": "AdminPass1!"},
    )
    login = await async_client.post("/api/v1/auth/login", json={"username": "chatadmin", "password": "AdminPass1!"})
    admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    groups = (await async_client.get("/api/v1/groups/", headers=admin_h)).json()
    gid = {g["name"]: g["id"] for g in groups}
    for name, group in (("op", "Operators"), ("viewer", "Viewers")):
        r = await async_client.post(
            "/api/v1/users/",
            headers=admin_h,
            json={"username": name, "password": "Userpass1!xx", "group_ids": [gid[group]]},
        )
        assert r.status_code in (200, 201), r.text
    users = {u.username: u for u in (await db_session.execute(select(User))).scalars().all()}
    p1 = await printer_factory(name="X1C-1")
    p2 = await printer_factory(name="Voron 2.4", model="P1S")
    lf = LibraryFile(
        filename="Benchy.gcode.3mf",
        file_path="/nonexistent/benchy.gcode.3mf",
        file_type="3mf",
        file_size=10,
        created_by_id=users["op"].id,
    )
    db_session.add(lf)
    await db_session.commit()
    return {
        "users": users,
        "p1": p1,
        "p2": p2,
        "p1_id": p1.id,
        "p2_id": p2.id,
        "file_id": lf.id,
        "op_id": users["op"].id,
        "admin_h": admin_h,
        "gid": gid,
    }


async def _link(agent, db_session, user, chat_user="U1", **kw):
    code, _ = await create_link_code(db_session, user.id, "test")
    replies = await agent.handle_message(msg(f"link {code}", user=chat_user, **kw))
    assert replies and replies[0].text.startswith("Linked"), replies
    return code


@pytest.fixture
def client_mock():
    client = MagicMock()
    client.pause_print.return_value = True
    client.resume_print.return_value = True
    client.stop_print.return_value = True
    with patch.object(printer_manager, "get_client", return_value=client):
        yield client


async def _chat_audit(db_session):
    db_session.expire_all()
    return (await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.actor_kind == "chat"))).scalars().all()


class TestChannelsAndLinking:
    async def test_not_allowed_channels_get_no_reply(self, farm):
        agent = ChatAgent(_cfg())
        assert await agent.handle_message(msg("status", channel="C9")) is None
        assert await agent.handle_message(msg("status", team="T9")) is None
        assert await agent.handle_message(msg("status", channel="D1", is_dm=True)) is None
        assert await agent.handle_message(msg("status", platform="discord", channel="901", team="5")) is None
        assert await ChatAgent(_cfg(enabled=False)).handle_message(msg("status")) is None
        assert await ChatAgent(_cfg(slack_enabled=False)).handle_message(msg("status")) is None

    async def test_dms_only_when_allowed(self, farm):
        agent = ChatAgent(_cfg(allow_dms=True))
        replies = await agent.handle_message(msg("status", channel="D1", is_dm=True))
        assert replies[0].text == LINK_TEXT
        # A DM from another workspace is still refused.
        assert await agent.handle_message(msg("status", channel="D1", team="T9", is_dm=True)) is None

    async def test_unlinked_users_only_get_link_reply(self, farm, client_mock):
        agent = ChatAgent(_cfg())
        for text in ("status", "help", "pause X1C-1", "cancel X1C-1", "yes"):
            replies = await agent.handle_message(msg(text))
            assert [r.text for r in replies] == [LINK_TEXT]
        client_mock.pause_print.assert_not_called()
        client_mock.stop_print.assert_not_called()

    async def test_link_code_single_use_and_expiry(self, farm, db_session):
        agent = ChatAgent(_cfg())
        code = await _link(agent, db_session, farm["users"]["op"])
        link = (await db_session.execute(select(ChatIdentityLink))).scalar_one()
        assert (link.platform, link.external_user_id, link.user_id) == ("slack", "U1", farm["users"]["op"].id)
        # Reused by someone else: refused.
        replies = await agent.handle_message(msg(f"link {code}", user="U2"))
        assert "not valid" in replies[0].text
        # Expired code: refused.
        code2, _ = await create_link_code(db_session, farm["users"]["viewer"].id, "test")
        row = (await db_session.execute(select(ChatLinkCode).where(ChatLinkCode.used_at.is_(None)))).scalar_one()
        row.expires_at = row.expires_at - timedelta(hours=1)
        await db_session.commit()
        replies = await agent.handle_message(msg(f"link {code2}", user="U3"))
        assert "not valid" in replies[0].text
        # Only a hash is stored.
        assert all(len(r.code_hash) == 64 for r in (await db_session.execute(select(ChatLinkCode))).scalars())
        kinds = [(e.route, e.status) for e in await _chat_audit(db_session)]
        assert ("chat:link", 200) in kinds and ("chat:link", 400) in kinds

    async def test_link_attempts_rate_limited(self, farm, db_session):
        agent = ChatAgent(_cfg())
        for _ in range(5):
            await agent.handle_message(msg("link AAAA-BBBB", user="U7"))
        code, _ = await create_link_code(db_session, farm["users"]["op"].id, "test")
        replies = await agent.handle_message(msg(f"link {code}", user="U7"))
        assert replies[0].text.startswith("Too many")

    async def test_unlink(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("unlink"))
        assert replies[0].text.startswith("Unlinked")
        assert (await agent.handle_message(msg("status")))[0].text == LINK_TEXT

    async def test_inactive_engine_user_is_unlinked(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        op = (await db_session.execute(select(User).where(User.username == "op"))).scalar_one()
        op.is_active = False
        await db_session.commit()
        assert (await agent.handle_message(msg("status")))[0].text == LINK_TEXT


class TestPermissions:
    async def test_viewer_cannot_act(self, farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["viewer"])
        for text in (
            "pause X1C-1",
            "resume X1C-1",
            "cancel X1C-1",
            "take X1C-1 out of service",
            "queue benchy on X1C-1",
        ):
            replies = await agent.handle_message(msg(text))
            assert "not allowed" in replies[0].text, (text, replies)
            assert replies[0].confirmation_id is None
        client_mock.pause_print.assert_not_called()
        client_mock.resume_print.assert_not_called()
        client_mock.stop_print.assert_not_called()
        p1 = (await db_session.execute(select(Printer).where(Printer.id == farm["p1_id"]))).scalar_one()
        assert not p1.out_of_service
        assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
        refused = [e for e in await _chat_audit(db_session) if e.status == 403]
        assert len(refused) == 5 and all(e.actor == "viewer" for e in refused)

    async def test_viewer_can_read(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["viewer"])
        replies = await agent.handle_message(msg("status"))
        assert "X1C-1: offline" in replies[0].text and "Voron 2.4: offline" in replies[0].text

    async def test_api_refusal_is_respected(self, farm, db_session, client_mock):
        """Even if the pre-check passed, a 403 from the API route stops the action."""
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        with patch("backend.app.services.chat_agent.agent._has_groups", return_value=True):
            # Strip the operator's printers:control from the group the API checks.
            from backend.app.models.group import Group

            ops = (await db_session.execute(select(Group).where(Group.name == "Operators"))).scalar_one()
            ops.permissions = [p for p in ops.permissions if p != "printers:control"]
            await db_session.commit()
            replies = await agent.handle_message(msg("pause X1C-1"))
        assert "not allowed" in replies[0].text
        client_mock.pause_print.assert_not_called()

    async def test_sign_in_off_still_uses_user_permissions(
        self, async_client, db_session, printer_factory, client_mock
    ):
        """With sign-in off the API checks nothing, so the agent's own check is what stops a viewer."""
        from backend.app.models.group import Group

        await printer_factory(name="X1C-1")
        viewers = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
        u = User(username="v2", password_hash="x", role="user", is_active=True)
        u.groups = [viewers]
        db_session.add(u)
        await db_session.commit()
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, u)
        replies = await agent.handle_message(msg("pause X1C-1"))
        assert "not allowed" in replies[0].text
        client_mock.pause_print.assert_not_called()


class TestActions:
    async def test_pause_and_resume_run_at_once(self, farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("pause x1c 1"))
        assert replies[0].text == "Paused the print on X1C-1." and replies[0].confirmation_id is None
        client_mock.pause_print.assert_called_once()
        replies = await agent.handle_message(msg("resume X1C-1"))
        assert replies[0].text == "Resumed the print on X1C-1."
        entries = await _chat_audit(db_session)
        assert [(e.route, e.status, e.actor) for e in entries if e.route != "chat:link"] == [
            ("chat:pause", 200, "op"),
            ("chat:resume", 200, "op"),
        ]
        # The underlying API calls were audited by the middleware as the operator too.
        api_rows = (
            (await db_session.execute(select(AuditLogEntry).where(AuditLogEntry.actor_kind == "user"))).scalars().all()
        )
        assert any(e.path == f"/api/v1/printers/{farm['p1_id']}/print/pause" and e.actor == "op" for e in api_rows)

    async def test_ambiguous_and_unknown_printer(self, farm, db_session, client_mock, printer_factory):
        await printer_factory(name="X1C-2")
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("pause x1c"))
        assert "more than one printer" in replies[0].text
        replies = await agent.handle_message(msg("pause prusa"))
        assert "do not see a printer" in replies[0].text
        replies = await agent.handle_message(msg("pause"))
        assert replies[0].text.startswith("Which printer?")
        client_mock.pause_print.assert_not_called()

    async def test_cancel_needs_confirmation_from_same_user(self, farm, db_session, client_mock):
        clock = Clock()
        agent = ChatAgent(_cfg(), clock=clock)
        await _link(agent, db_session, farm["users"]["op"])
        await _link(agent, db_session, farm["users"]["op"], chat_user="U2")
        replies = await agent.handle_message(msg("cancel X1C-1"))
        conf = replies[0].confirmation_id
        assert conf and "Please confirm" in replies[0].text
        client_mock.stop_print.assert_not_called()
        # Someone else presses the button: refused, request stays open.
        other = await agent.handle_button("slack", "U2", "C1", conf, True, team_id="T1")
        assert other.private and "Only the person" in other.text
        # Someone else types yes: there is nothing of theirs to confirm.
        assert "nothing waiting" in (await agent.handle_message(msg("yes", user="U2")))[0].text
        client_mock.stop_print.assert_not_called()
        # The asker confirms.
        replies = await agent.handle_message(msg("yes"))
        assert replies[0].text == "Cancelled the print on X1C-1."
        client_mock.stop_print.assert_called_once()
        # A second press of the same button does nothing.
        again = await agent.handle_button("slack", "U1", "C1", conf, True, team_id="T1")
        assert "no longer waiting" in again.text
        client_mock.stop_print.assert_called_once()
        routes = [(e.route, e.status) for e in await _chat_audit(db_session) if e.route == "chat:cancel"]
        assert routes == [("chat:cancel", 202), ("chat:cancel", 200)]

    async def test_confirmation_expires(self, farm, db_session, client_mock):
        clock = Clock()
        agent = ChatAgent(_cfg(), clock=clock)
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("cancel X1C-1"))
        clock.t += 121
        reply = await agent.handle_button("slack", "U1", "C1", replies[0].confirmation_id, True, team_id="T1")
        assert "expired" in reply.text
        client_mock.stop_print.assert_not_called()
        assert ("chat:cancel", 408) in [(e.route, e.status) for e in await _chat_audit(db_session)]

    async def test_decline(self, farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        await agent.handle_message(msg("cancel X1C-1"))
        assert (await agent.handle_message(msg("no")))[0].text == "OK, nothing was changed."
        client_mock.stop_print.assert_not_called()

    async def test_permission_rechecked_on_confirm(self, farm, db_session, client_mock):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("cancel X1C-1"))
        assert replies[0].confirmation_id
        from backend.app.models.group import Group

        op = (await db_session.execute(select(User).where(User.username == "op"))).scalar_one()
        op.groups = [(await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()]
        await db_session.commit()
        replies = await agent.handle_message(msg("yes"))
        assert "no longer allowed" in replies[0].text
        client_mock.stop_print.assert_not_called()

    async def test_out_of_service_confirmed_then_back(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("take voron out of service because bed adhesion"))
        assert replies[0].confirmation_id
        p2 = (await db_session.execute(select(Printer).where(Printer.id == farm["p2_id"]))).scalar_one()
        assert not p2.out_of_service
        reply = await agent.handle_button("slack", "U1", "C1", replies[0].confirmation_id, True, team_id="T1")
        assert "is out of service" in reply.text
        db_session.expire_all()
        p2 = (await db_session.execute(select(Printer).where(Printer.id == farm["p2_id"]))).scalar_one()
        assert p2.out_of_service and p2.out_of_service_reason == "bed adhesion"
        # Back in service needs no confirmation.
        replies = await agent.handle_message(msg("put voron back in service"))
        assert replies[0].text == "Voron 2.4 is back in service." and replies[0].confirmation_id is None
        db_session.expire_all()
        p2 = (await db_session.execute(select(Printer).where(Printer.id == farm["p2_id"]))).scalar_one()
        assert not p2.out_of_service

    async def test_queue_library_file(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("queue benchy on X1C-1"))
        assert replies[0].text.startswith("Queued Benchy.gcode.3mf on X1C-1"), replies
        item = (await db_session.execute(select(PrintQueueItem))).scalar_one()
        assert item.library_file_id == farm["file_id"] and item.printer_id == farm["p1_id"]
        assert item.created_by_id == farm["op_id"]
        replies = await agent.handle_message(msg("what is waiting in the queue"))
        assert "Benchy.gcode.3mf -> X1C-1" in replies[0].text
        replies = await agent.handle_message(msg("queue benchy"))
        assert replies[0].text.startswith("Where should")

    async def test_status_reports_progress(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["viewer"])
        with patch("backend.app.services.chat_agent.engine_api.EngineApi.printer_status", new_callable=AsyncMock) as st:
            st.return_value = {
                "connected": True,
                "state": "RUNNING",
                "subtask_name": "gearbox",
                "progress": 42.4,
                "remaining_time": 65,
                "layer_num": 10,
                "total_layers": 200,
                "hms_errors": [],
            }
            replies = await agent.handle_message(msg("how is the X1C-1 doing?"))
        assert replies[0].text == "X1C-1: printing gearbox 42% (layer 10/200), about 1h 5m left"


class TestVisibilityAndLlm:
    async def test_printer_visibility_goes_through_one_hook(self, farm, db_session, client_mock, monkeypatch):
        hidden = farm["p1_id"]
        monkeypatch.setattr(
            engine_api, "filter_visible_printers", lambda user, printers: [p for p in printers if p["id"] != hidden]
        )
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        assert "do not see a printer" in (await agent.handle_message(msg("pause X1C-1")))[0].text
        status = (await agent.handle_message(msg("status")))[0].text
        assert "X1C-1" not in status and "Voron 2.4" in status
        client_mock.pause_print.assert_not_called()

    async def test_printer_access_code_never_reaches_chat(self, farm, db_session):
        agent = ChatAgent(_cfg())
        await _link(agent, db_session, farm["users"]["op"])
        user = (await db_session.execute(select(User).where(User.username == "chatadmin"))).scalar_one()
        api = engine_api.EngineApi(user, "chat:test")
        printers = await api.visible_printers()
        assert printers and all("access_code" not in p for p in printers)

    async def test_llm_only_when_parser_fails(self, farm, db_session, client_mock):
        calls = []

        async def fake_llm(text, **kw):
            calls.append(text)
            return ParsedIntent(Intent.PAUSE, printer="voron", source="llm")

        agent = ChatAgent(
            _cfg(llm_enabled=True, llm_base_url="http://llm.test/v1", llm_model="m"), llm_classify=fake_llm
        )
        await _link(agent, db_session, farm["users"]["op"])
        replies = await agent.handle_message(msg("get the corexy to take a breather"))
        assert replies[0].text == "Paused the print on Voron 2.4."
        assert calls == ["get the corexy to take a breather"]
        await agent.handle_message(msg("status"))
        assert len(calls) == 1  # parsed messages never reach the model

    async def test_llm_cannot_skip_confirmation_or_permissions(self, farm, db_session, client_mock):
        async def fake_llm(text, **kw):
            return ParsedIntent(Intent.CANCEL, printer="X1C-1", source="llm")

        agent = ChatAgent(
            _cfg(llm_enabled=True, llm_base_url="http://llm.test/v1", llm_model="m"), llm_classify=fake_llm
        )
        await _link(agent, db_session, farm["users"]["viewer"])
        replies = await agent.handle_message(msg("nuke the x1c job"))
        assert "not allowed" in replies[0].text
        await _link(agent, db_session, farm["users"]["op"], chat_user="U5")
        replies = await agent.handle_message(msg("nuke the x1c job", user="U5"))
        assert replies[0].confirmation_id
        client_mock.stop_print.assert_not_called()
