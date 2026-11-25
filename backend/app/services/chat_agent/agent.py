"""Platform-neutral chat agent: who may ask what, confirmations, linking, audit.

Adapters turn a Slack or Discord event into an ``IncomingMessage`` and send back the
``Reply`` objects. A reply with ``confirmation_id`` set asks the adapter to show
Confirm / Cancel buttons; typing "yes" or "no" works too.

Rules enforced here, for every message:

1. Only configured channels (and, when set, workspaces / guilds) are served; direct
   messages only when ``allow_dms`` is on. Anything else is ignored without a reply.
2. A chat account that is not linked to an active engine user gets only the
   "link your account" reply. Linking uses a single-use code made in the engine UI.
3. Each intent needs the same permissions as the API route it uses; the call itself
   then goes through that API route as the linked user (engine_api.py).
4. Cancel and out-of-service wait for the same chat user to confirm within the
   configured window; queue, pause, resume and back-in-service run at once.
5. Every action, refusal, confirmation and link change is written to the audit log.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from backend.app.core import database
from backend.app.core.permissions import Permission
from backend.app.models.chat_agent import ChatIdentityLink, ChatLinkCode
from backend.app.models.user import User
from backend.app.services import audit
from backend.app.services.chat_agent import llm
from backend.app.services.chat_agent.config import ChatAgentConfig
from backend.app.services.chat_agent.engine_api import EngineApi, EngineApiError
from backend.app.services.chat_agent.intents import (
    CONFIRM_INTENTS,
    Intent,
    ParsedIntent,
    _norm,
    best_match,
    parse,
)

logger = logging.getLogger(__name__)

LINK_CODE_TTL = timedelta(minutes=10)
LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LINK_ATTEMPTS_MAX = 5
LINK_ATTEMPTS_WINDOW = 15 * 60

# Each entry is a list of groups; the user needs at least one permission from every
# group. They mirror the dependencies of the API routes each intent calls.
_P = Permission
REQUIRED: dict[Intent, list[tuple[str, ...]]] = {
    Intent.STATUS: [(_P.PRINTERS_READ,)],
    Intent.ALARMS: [(_P.PRINTERS_READ,)],
    Intent.FAILURES: [(_P.ARCHIVES_READ_ALL, _P.ARCHIVES_READ_OWN)],
    Intent.QUEUE_LIST: [(_P.QUEUE_READ_ALL, _P.QUEUE_READ_OWN)],
    Intent.QUEUE_FILE: [(_P.QUEUE_CREATE,), (_P.LIBRARY_READ_ALL, _P.LIBRARY_READ_OWN), (_P.PRINTERS_READ,)],
    Intent.PAUSE: [(_P.PRINTERS_CONTROL,), (_P.PRINTERS_READ,)],
    Intent.RESUME: [(_P.PRINTERS_CONTROL,), (_P.PRINTERS_READ,)],
    Intent.CANCEL: [(_P.PRINTERS_CONTROL,), (_P.PRINTERS_READ,)],
    Intent.OUT_OF_SERVICE: [(_P.PRINTERS_UPDATE,), (_P.PRINTERS_READ,)],
    Intent.BACK_IN_SERVICE: [(_P.PRINTERS_UPDATE,), (_P.PRINTERS_READ,)],
}

_ACTION_VERB = {
    Intent.QUEUE_FILE: "queue prints",
    Intent.PAUSE: "pause prints",
    Intent.RESUME: "resume prints",
    Intent.CANCEL: "cancel prints",
    Intent.OUT_OF_SERVICE: "take printers out of service",
    Intent.BACK_IN_SERVICE: "put printers back in service",
    Intent.STATUS: "see printer status",
    Intent.ALARMS: "see printer alarms",
    Intent.FAILURES: "see the print log",
    Intent.QUEUE_LIST: "see the queue",
}

HELP_TEXT = (
    "I can tell you:\n"
    "- status [printer]: what is printing, progress and time left\n"
    "- queue: what is waiting to print\n"
    "- failures: recent failed prints and why\n"
    "- alarms [printer]: active printer alarms\n"
    "And, if your account allows it:\n"
    "- queue <file> on <printer>, or queue <file> on any <model>\n"
    "- pause <printer> / resume <printer>\n"
    "- cancel <printer> (asks you to confirm)\n"
    "- take <printer> out of service [because <reason>] (asks you to confirm)\n"
    "- put <printer> back in service\n"
    "- unlink: disconnect this chat account from your engine account"
)

LINK_TEXT = (
    "Your chat account is not linked to a print farm account yet. In the engine web UI open "
    "Settings > Chat Agent, choose Link a chat account, and send me the code it shows, like: link ABCD-EFGH"
)


@dataclass
class IncomingMessage:
    platform: str  # "slack" | "discord"
    user_id: str
    channel_id: str
    text: str
    team_id: str | None = None  # Slack workspace id / Discord guild id
    is_dm: bool = False
    user_name: str | None = None


@dataclass
class Reply:
    text: str
    confirmation_id: str | None = None
    # Show only to the person it is for (a Slack ephemeral, a Discord ephemeral response).
    private: bool = False


@dataclass
class PendingConfirmation:
    id: str
    platform: str
    external_user_id: str
    channel_id: str
    engine_user_id: int
    intent: Intent
    printer_id: int
    printer_name: str
    reason: str | None
    summary: str
    expires_at: float


@dataclass
class _Ctx:
    msg: IncomingMessage
    user: User
    api: EngineApi
    printers: list[dict] | None = field(default=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def hash_code(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


async def create_link_code(db, user_id: int, created_by: str | None) -> tuple[str, datetime]:
    """Make a single-use link code for ``user_id``. Returns (code, expires_at UTC)."""
    now = _utcnow()
    await db.execute(delete(ChatLinkCode).where(ChatLinkCode.expires_at < now))
    raw = "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(8))
    code = f"{raw[:4]}-{raw[4:]}"
    expires = now + LINK_CODE_TTL
    db.add(ChatLinkCode(code_hash=hash_code(code), user_id=user_id, created_by=created_by, expires_at=expires))
    await db.commit()
    return code, expires


def fmt_duration(seconds: int | float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds <= 0:
        return None
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{max(m, 1)}m"


def _has_groups(user: User, groups: list[tuple[str, ...]]) -> bool:
    return all(user.has_any_permission(*[str(p) for p in group]) for group in groups)


def _needs_text(groups: list[tuple[str, ...]]) -> str:
    return " and ".join(" or ".join(str(p) for p in g) for g in groups)


class ChatAgent:
    def __init__(self, config: ChatAgentConfig, *, api_factory=None, llm_classify=None, clock=time.monotonic):
        self.config = config
        self._api_factory = api_factory or (lambda user, label: EngineApi(user, label))
        self._llm_classify = llm_classify or llm.classify
        self._clock = clock
        self._pending: dict[str, PendingConfirmation] = {}
        self._link_failures: dict[tuple[str, str], list[float]] = {}

    # --- channel restriction ---------------------------------------------------

    def is_allowed(self, msg: IncomingMessage) -> bool:
        if not self.config.enabled:
            return False
        if msg.platform == "slack" and not self.config.slack_enabled:
            return False
        if msg.platform == "discord" and not self.config.discord_enabled:
            return False
        teams = self.config.allowed_teams(msg.platform)
        if msg.is_dm:
            if not self.config.allow_dms:
                return False
            # A Discord DM carries no guild; a Slack DM still names its workspace.
            return not (teams and msg.team_id is not None and msg.team_id not in teams)
        if teams and msg.team_id not in teams:
            return False
        return msg.channel_id in self.config.allowed_channels(msg.platform)

    # --- entry points ------------------------------------------------------------

    async def handle_message(self, msg: IncomingMessage) -> list[Reply] | None:
        """Answer one message. None means "not ours": send nothing."""
        if not self.is_allowed(msg):
            return None
        parsed = parse(msg.text)
        if parsed.intent == Intent.LINK:
            return [await self._link(msg, parsed.code or "")]

        user = await self._linked_user(msg.platform, msg.user_id)
        if user is None:
            return [Reply(LINK_TEXT)]
        ctx = _Ctx(msg=msg, user=user, api=self._api_factory(user, f"chat:{msg.platform}:{user.username}"))

        if parsed.intent == Intent.UNKNOWN and self.config.llm_ready:
            parsed = await self._llm_classify(
                msg.text,
                base_url=self.config.llm_base_url,
                model=self.config.llm_model,
                api_key=self.config.llm_api_key,
            )
        try:
            return await self._dispatch(ctx, parsed)
        except EngineApiError as e:
            return [Reply(self._api_error_text(e))]

    async def handle_button(
        self,
        platform: str,
        user_id: str,
        channel_id: str,
        confirmation_id: str,
        accept: bool,
        *,
        team_id: str | None = None,
        is_dm: bool = False,
    ) -> Reply | None:
        """A Confirm / Cancel button press."""
        msg = IncomingMessage(platform, user_id, channel_id, "", team_id=team_id, is_dm=is_dm)
        if not self.is_allowed(msg):
            return None
        pending = self._pending.get(confirmation_id)
        if pending is None:
            return Reply("That request is no longer waiting for confirmation.")
        if pending.platform != platform or pending.external_user_id != user_id:
            return Reply("Only the person who asked can confirm or cancel this.", private=True)
        return await self._resolve(pending, accept)

    # --- linking -----------------------------------------------------------------

    def _link_rate_limited(self, msg: IncomingMessage) -> bool:
        key = (msg.platform, msg.user_id)
        now = self._clock()
        recent = [t for t in self._link_failures.get(key, []) if now - t < LINK_ATTEMPTS_WINDOW]
        self._link_failures[key] = recent
        return len(recent) >= LINK_ATTEMPTS_MAX

    async def _link(self, msg: IncomingMessage, code: str) -> Reply:
        if self._link_rate_limited(msg):
            return Reply("Too many wrong codes. Wait a few minutes and make a new code.")
        now = _utcnow()
        async with database.async_session() as db:
            row = (
                await db.execute(select(ChatLinkCode).where(ChatLinkCode.code_hash == hash_code(code)))
            ).scalar_one_or_none()
            if row is None or row.used_at is not None or row.expires_at < now:
                self._link_failures.setdefault((msg.platform, msg.user_id), []).append(self._clock())
                await self._audit(None, msg, "link", "invalid or expired link code", 400)
                return Reply("That code is not valid or has expired. Make a new one in Settings > Chat Agent.")
            user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
            if user is None or not user.is_active:
                return Reply("That engine account is not active.")
            row.used_at = now
            link = (
                await db.execute(
                    select(ChatIdentityLink).where(
                        ChatIdentityLink.platform == msg.platform,
                        ChatIdentityLink.external_user_id == msg.user_id,
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                link = ChatIdentityLink(platform=msg.platform, external_user_id=msg.user_id)
                db.add(link)
            link.user_id = user.id
            link.external_team_id = msg.team_id
            link.display_name = (msg.user_name or "")[:200] or None
            await db.commit()
            username = user.username
        self._link_failures.pop((msg.platform, msg.user_id), None)
        await self._audit(username, msg, "link", f"linked to {username}", 200)
        return Reply(f"Linked. I will act as {username} for you. Say help to see what I can do.")

    async def _linked_user(self, platform: str, external_user_id: str) -> User | None:
        async with database.async_session() as db:
            link = (
                await db.execute(
                    select(ChatIdentityLink).where(
                        ChatIdentityLink.platform == platform,
                        ChatIdentityLink.external_user_id == external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                return None
            user = (await db.execute(select(User).where(User.id == link.user_id))).scalar_one_or_none()
            if user is None or not user.is_active:
                return None
            link.last_used_at = _utcnow()
            await db.commit()
            # Group permissions are loaded with the user (selectin); detach for use outside the session.
            _ = [g.permissions for g in user.groups]
            return user

    async def _unlink(self, ctx: _Ctx) -> list[Reply]:
        async with database.async_session() as db:
            await db.execute(
                delete(ChatIdentityLink).where(
                    ChatIdentityLink.platform == ctx.msg.platform,
                    ChatIdentityLink.external_user_id == ctx.msg.user_id,
                )
            )
            await db.commit()
        await self._audit(ctx.user.username, ctx.msg, "unlink", "chat account unlinked", 200)
        return [Reply("Unlinked. I will not act for this chat account any more.")]

    # --- dispatch ------------------------------------------------------------------

    async def _dispatch(self, ctx: _Ctx, p: ParsedIntent) -> list[Reply]:
        i = p.intent
        if i == Intent.HELP:
            return [Reply(HELP_TEXT)]
        if i == Intent.UNKNOWN:
            return [Reply("Sorry, I did not understand that. Say help to see what I can do.")]
        if i == Intent.UNLINK:
            return await self._unlink(ctx)
        if i == Intent.CONFIRM:
            return [await self._confirm_text(ctx, True)]
        if i == Intent.DECLINE:
            return [await self._confirm_text(ctx, False)]
        if i == Intent.LINK:  # handled before dispatch; an LLM can never produce it
            return [Reply(LINK_TEXT)]

        groups = REQUIRED[i]
        if not _has_groups(ctx.user, groups):
            if i in _ACTION_VERB and i not in (Intent.STATUS, Intent.ALARMS, Intent.FAILURES, Intent.QUEUE_LIST):
                await self._audit(ctx.user.username, ctx.msg, i.value, "refused: missing permission", 403)
            return [Reply(f"Your account is not allowed to {_ACTION_VERB[i]} (needs {_needs_text(groups)}).")]

        if i == Intent.STATUS:
            return [await self._status(ctx, p)]
        if i == Intent.QUEUE_LIST:
            return [await self._queue_list(ctx, p)]
        if i == Intent.FAILURES:
            return [await self._failures(ctx, p)]
        if i == Intent.ALARMS:
            return [await self._alarms(ctx, p)]
        if i == Intent.QUEUE_FILE:
            return [await self._queue_file(ctx, p)]
        return [await self._printer_action(ctx, p)]

    # --- helpers ---------------------------------------------------------------------

    async def _printers(self, ctx: _Ctx) -> list[dict]:
        if ctx.printers is None:
            ctx.printers = await ctx.api.visible_printers()
        return ctx.printers

    async def _resolve_printer(self, ctx: _Ctx, name: str | None) -> tuple[dict | None, str | None]:
        """(printer, None) or (None, reply text)."""
        printers = await self._printers(ctx)
        if not name:
            # Fall back to a printer named anywhere in the message.
            hits = [pr for pr in printers if _norm(pr.get("name", "")) and _norm(pr["name"]) in _norm(ctx.msg.text)]
            if len(hits) == 1:
                return hits[0], None
            return None, "Which printer? " + self._names_hint(printers)
        m = best_match(name, printers, ("name",))
        if m.item is not None:
            return m.item, None
        if m.ambiguous:
            names = ", ".join(c["name"] for c in m.candidates[:6])
            return None, f'"{name}" matches more than one printer: {names}. Which one?'
        return None, f'I do not see a printer called "{name}". ' + self._names_hint(printers)

    @staticmethod
    def _names_hint(printers: list[dict]) -> str:
        if not printers:
            return "You have no printers I can see."
        names = ", ".join(p["name"] for p in printers[:12])
        more = "" if len(printers) <= 12 else f" and {len(printers) - 12} more"
        return f"Printers: {names}{more}."

    async def _read_scope(self, ctx: _Ctx, p: ParsedIntent) -> tuple[list[dict], dict | None, str | None]:
        """Visible printers, the one asked about (if any), or an error reply."""
        printers = await self._printers(ctx)
        target = None
        if p.printer:
            target, err = await self._resolve_printer(ctx, p.printer)
            if err:
                return printers, None, err
        else:
            hits = [pr for pr in printers if _norm(pr.get("name", "")) and _norm(pr["name"]) in _norm(ctx.msg.text)]
            if len(hits) == 1:
                target = hits[0]
        return printers, target, None

    @staticmethod
    def _api_error_text(e: EngineApiError) -> str:
        if e.status in (401, 403):
            return "Your account is not allowed to do that."
        if e.status == 404:
            return "Not found. It may have been removed."
        return f"The engine refused that: {e.detail}"

    async def _audit(self, actor: str | None, msg: IncomingMessage, what: str, summary: str, status: int) -> None:
        if not audit.ENABLED:
            return
        await audit.write(
            {
                "actor": (actor or f"{msg.platform}:{msg.user_id}")[:200],
                "actor_kind": "chat",
                "ip": None,
                "method": "CHAT",
                "route": f"chat:{what}"[:300],
                "path": f"{msg.platform}:{msg.user_id} {summary}"[:500],
                "status": int(status),
                "duration_ms": None,
            }
        )

    # --- reads -----------------------------------------------------------------------

    async def _status(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        printers, target, err = await self._read_scope(ctx, p)
        if err:
            return Reply(err)
        if not printers:
            return Reply("You have no printers I can see.")
        lines = []
        for pr in [target] if target else printers:
            try:
                st = await ctx.api.printer_status(pr["id"])
            except EngineApiError:
                st = {"connected": False}
            lines.append(self._status_line(pr, st))
        return Reply("\n".join(lines))

    @staticmethod
    def _status_line(pr: dict, st: dict) -> str:
        name = pr.get("name", f"#{pr.get('id')}")
        oos = " [out of service]" if pr.get("out_of_service") else ""
        if not pr.get("is_active", True):
            return f"{name}: disabled{oos}"
        if not st.get("connected"):
            return f"{name}: offline{oos}"
        state = (st.get("state") or "").upper()
        job = st.get("subtask_name") or st.get("current_print") or st.get("gcode_file")
        errors = len(st.get("hms_errors") or [])
        err_txt = f", {errors} alarm{'s' if errors != 1 else ''}" if errors else ""
        if state in ("RUNNING", "PREPARE", "SLICING", "PAUSE"):
            pct = st.get("progress")
            parts = [f"{name}: {'paused' if state == 'PAUSE' else 'printing'}"]
            if job:
                parts.append(f" {job}")
            if pct is not None:
                parts.append(f" {round(float(pct))}%")
            if st.get("layer_num") and st.get("total_layers"):
                parts.append(f" (layer {st['layer_num']}/{st['total_layers']})")
            left = fmt_duration((st.get("remaining_time") or 0) * 60)
            if left and state != "PAUSE":
                parts.append(f", about {left} left")
            return "".join(parts) + err_txt + oos
        if state == "FINISH":
            return f"{name}: finished{(' ' + job) if job else ''}, waiting for the plate to be cleared{err_txt}{oos}"
        if state == "FAILED":
            return f"{name}: last print failed{(' (' + job + ')') if job else ''}{err_txt}{oos}"
        return f"{name}: idle{err_txt}{oos}"

    async def _queue_list(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        printers, target, err = await self._read_scope(ctx, p)
        if err:
            return Reply(err)
        visible = {pr["id"]: pr["name"] for pr in printers}
        items = await ctx.api.queue_items()
        if not ctx.user.has_permission(Permission.QUEUE_READ_ALL):
            items = [it for it in items if it.get("created_by_id") == ctx.user.id]
        items = [it for it in items if it.get("printer_id") is None or it.get("printer_id") in visible]
        if target:
            items = [it for it in items if it.get("printer_id") == target["id"]]
        if not items:
            return Reply("Nothing is waiting in the queue" + (f" for {target['name']}." if target else "."))
        items.sort(key=lambda it: (-(it.get("priority") or 50), it.get("position") or 0))
        lines = [f"{len(items)} waiting:"]
        for it in items[:10]:
            name = it.get("library_file_name") or it.get("archive_name") or f"item #{it['id']}"
            if it.get("printer_id") is not None:
                where = visible.get(it["printer_id"], it.get("printer_name") or "a printer")
            elif it.get("target_model"):
                where = f"any {it['target_model']}"
            elif it.get("target_group_name"):
                where = f"group {it['target_group_name']}"
            else:
                where = "no printer yet"
            extra = f" ({it['waiting_reason']})" if it.get("waiting_reason") else ""
            lines.append(f"- {name} -> {where}{extra}")
        if len(items) > 10:
            lines.append(f"... and {len(items) - 10} more")
        return Reply("\n".join(lines))

    async def _failures(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        printers, target, err = await self._read_scope(ctx, p)
        if err:
            return Reply(err)
        visible = {pr["id"] for pr in printers}
        rows = await ctx.api.recent_failures(limit=25, printer_id=target["id"] if target else None)
        if not ctx.user.has_permission(Permission.ARCHIVES_READ_ALL):
            rows = [r for r in rows if r.get("created_by_id") == ctx.user.id]
        rows = [r for r in rows if r.get("printer_id") is None or r.get("printer_id") in visible][:5]
        if not rows:
            return Reply("No failed prints recently" + (f" on {target['name']}." if target else "."))
        lines = ["Recent failures:"]
        for r in rows:
            when = (r.get("completed_at") or r.get("created_at") or "")[:16].replace("T", " ")
            reason = r.get("failure_reason") or "no reason recorded"
            lines.append(
                f"- {r.get('print_name') or 'print'} on {r.get('printer_name') or 'a printer'}, {when}: {reason}"
            )
        return Reply("\n".join(lines))

    async def _alarms(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        printers, target, err = await self._read_scope(ctx, p)
        if err:
            return Reply(err)
        visible = {pr["id"] for pr in printers}
        rows = await ctx.api.active_alarms(printer_id=target["id"] if target else None)
        rows = [r for r in rows if r.get("printer_id") in visible]
        if not rows:
            return Reply("No active alarms" + (f" on {target['name']}." if target else "."))
        lines = [f"{len(rows)} active alarm{'s' if len(rows) != 1 else ''}:"]
        for r in rows[:10]:
            lines.append(f"- {r.get('printer_name') or 'printer'} [{r.get('severity')}]: {r.get('message')}")
        if len(rows) > 10:
            lines.append(f"... and {len(rows) - 10} more")
        return Reply("\n".join(lines))

    # --- actions ---------------------------------------------------------------------

    async def _queue_file(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        if not p.file:
            return Reply("Which file? Say: queue <file> on <printer>")
        files = await ctx.api.library_files()
        m = best_match(p.file, files, ("filename", "print_name"))
        if m.item is None:
            if m.ambiguous:
                names = ", ".join(f["filename"] for f in m.candidates[:6])
                return Reply(f'"{p.file}" matches more than one file: {names}. Which one?')
            return Reply(f'I do not see a library file called "{p.file}".')
        f = m.item
        printer_id, where = None, None
        if p.printer:
            pr, err = await self._resolve_printer(ctx, p.printer)
            if err:
                return Reply(err)
            if pr.get("out_of_service"):
                return Reply(f"{pr['name']} is out of service. Put it back in service first, or pick another printer.")
            printer_id, where = pr["id"], pr["name"]
        elif p.model:
            where = f"any {p.model}"
        else:
            return Reply(f"Where should {f['filename']} print? Say: queue {p.file} on <printer>, or on any <model>")
        try:
            item = await ctx.api.queue_file(f["id"], printer_id, p.model if printer_id is None else None)
        except EngineApiError as e:
            await self._audit(ctx.user.username, ctx.msg, "queue_file", f"{f['filename']} -> {where}", e.status)
            return Reply(self._api_error_text(e))
        await self._audit(
            ctx.user.username, ctx.msg, "queue_file", f"{f['filename']} -> {where} (queue item {item.get('id')})", 200
        )
        return Reply(f"Queued {f['filename']} on {where} (queue item #{item.get('id')}).")

    async def _printer_action(self, ctx: _Ctx, p: ParsedIntent) -> Reply:
        pr, err = await self._resolve_printer(ctx, p.printer)
        if err:
            return Reply(err)
        if p.intent in CONFIRM_INTENTS:
            return await self._ask_confirmation(ctx, p, pr)
        return await self._run_action(ctx.user, ctx.api, ctx.msg, p.intent, pr["id"], pr["name"], p.reason)

    async def _ask_confirmation(self, ctx: _Ctx, p: ParsedIntent, pr: dict) -> Reply:
        if p.intent == Intent.CANCEL:
            job = ""
            try:
                st = await ctx.api.printer_status(pr["id"])
                if st.get("state") in ("RUNNING", "PAUSE", "PREPARE"):
                    name = st.get("subtask_name") or st.get("current_print") or "the current print"
                    pct = st.get("progress")
                    job = f" {name}" + (f" ({round(float(pct))}%)" if pct is not None else "")
                elif st.get("connected"):
                    return Reply(f"{pr['name']} is not printing right now.")
            except EngineApiError:
                pass
            summary = f"cancel the print{job or ''} on {pr['name']}"
        else:
            reason = f" (reason: {p.reason})" if p.reason else ""
            summary = f"take {pr['name']} out of service{reason}; a running print continues, no new jobs start"
        self._expire()
        # One open question per person per channel: a new one replaces the old.
        for key, old in list(self._pending.items()):
            if (old.platform, old.external_user_id, old.channel_id) == (
                ctx.msg.platform,
                ctx.msg.user_id,
                ctx.msg.channel_id,
            ):
                del self._pending[key]
        pending = PendingConfirmation(
            id=secrets.token_urlsafe(12),
            platform=ctx.msg.platform,
            external_user_id=ctx.msg.user_id,
            channel_id=ctx.msg.channel_id,
            engine_user_id=ctx.user.id,
            intent=p.intent,
            printer_id=pr["id"],
            printer_name=pr["name"],
            reason=p.reason,
            summary=summary,
            expires_at=self._clock() + self.config.confirm_timeout_seconds,
        )
        self._pending[pending.id] = pending
        await self._audit(ctx.user.username, ctx.msg, p.intent.value, f"awaiting confirmation: {summary}", 202)
        window = fmt_duration(self.config.confirm_timeout_seconds)
        return Reply(
            f"Please confirm: {summary}. Reply yes to go ahead or no to stop (within {window}).",
            confirmation_id=pending.id,
        )

    def _expire(self) -> None:
        now = self._clock()
        for key, p in list(self._pending.items()):
            if p.expires_at < now - 3600:  # forget long-dead ones; recent ones answer "expired"
                del self._pending[key]

    async def _confirm_text(self, ctx: _Ctx, accept: bool) -> Reply:
        mine = [
            p
            for p in self._pending.values()
            if (p.platform, p.external_user_id, p.channel_id) == (ctx.msg.platform, ctx.msg.user_id, ctx.msg.channel_id)
        ]
        if not mine:
            low = _norm(ctx.msg.text)
            if not accept and low in ("cancel", "stop", "abort"):
                return Reply("Cancel which printer? Say: cancel <printer>")
            return Reply("There is nothing waiting for your confirmation.")
        return await self._resolve(mine[-1], accept)

    async def _resolve(self, pending: PendingConfirmation, accept: bool) -> Reply:
        self._pending.pop(pending.id, None)
        msg = IncomingMessage(pending.platform, pending.external_user_id, pending.channel_id, "")
        user = await self._linked_user(pending.platform, pending.external_user_id)
        if user is None or user.id != pending.engine_user_id:
            return Reply(LINK_TEXT)
        if self._clock() > pending.expires_at:
            await self._audit(user.username, msg, pending.intent.value, f"expired: {pending.summary}", 408)
            return Reply("That request expired, nothing was changed. Ask again if you still want it.")
        if not accept:
            await self._audit(user.username, msg, pending.intent.value, f"declined: {pending.summary}", 200)
            return Reply("OK, nothing was changed.")
        # Permissions are checked again: they may have changed while we waited.
        if not _has_groups(user, REQUIRED[pending.intent]):
            await self._audit(user.username, msg, pending.intent.value, "refused: missing permission", 403)
            return Reply(f"Your account is no longer allowed to {_ACTION_VERB[pending.intent]}.")
        api = self._api_factory(user, f"chat:{pending.platform}:{user.username}")
        return await self._run_action(
            user, api, msg, pending.intent, pending.printer_id, pending.printer_name, pending.reason
        )

    async def _run_action(
        self,
        user: User,
        api: EngineApi,
        msg: IncomingMessage,
        intent: Intent,
        printer_id: int,
        printer_name: str,
        reason: str | None,
    ) -> Reply:
        calls = {
            Intent.PAUSE: (api.pause, (printer_id,), f"Paused the print on {printer_name}."),
            Intent.RESUME: (api.resume, (printer_id,), f"Resumed the print on {printer_name}."),
            Intent.CANCEL: (api.cancel, (printer_id,), f"Cancelled the print on {printer_name}."),
            Intent.OUT_OF_SERVICE: (
                api.out_of_service,
                (printer_id, reason),
                f"{printer_name} is out of service. It gets no new jobs until you put it back in service.",
            ),
            Intent.BACK_IN_SERVICE: (
                api.back_in_service,
                (printer_id,),
                f"{printer_name} is back in service.",
            ),
        }
        fn, args, done = calls[intent]
        summary = f"{intent.value} printer {printer_id} ({printer_name})" + (f" reason: {reason}" if reason else "")
        try:
            await fn(*args)
        except EngineApiError as e:
            await self._audit(user.username, msg, intent.value, summary, e.status)
            return Reply(self._api_error_text(e))
        await self._audit(user.username, msg, intent.value, summary, 200)
        return Reply(done)
