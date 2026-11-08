"""The fixed set of things the chat agent can do, and the deterministic parser.

The parser is the primary way messages are understood: plain keyword and pattern
rules, no model. It returns raw names ("the x1c", "benchy"); the agent resolves them
against what the user can see with ``best_match``. The optional LLM (llm.py) is only
consulted when this parser returns UNKNOWN, and it can only choose from
``LLM_INTENTS``; whatever it answers is resolved and permission-checked the same way.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    HELP = "help"
    LINK = "link"
    UNLINK = "unlink"
    CONFIRM = "confirm"
    DECLINE = "decline"
    # Reads
    STATUS = "status"
    FAILURES = "failures"
    QUEUE_LIST = "queue_list"
    ALARMS = "alarms"
    # Actions
    QUEUE_FILE = "queue_file"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    OUT_OF_SERVICE = "out_of_service"
    BACK_IN_SERVICE = "back_in_service"
    UNKNOWN = "unknown"


READ_INTENTS = frozenset({Intent.STATUS, Intent.FAILURES, Intent.QUEUE_LIST, Intent.ALARMS})
ACTION_INTENTS = frozenset(
    {
        Intent.QUEUE_FILE,
        Intent.PAUSE,
        Intent.RESUME,
        Intent.CANCEL,
        Intent.OUT_OF_SERVICE,
        Intent.BACK_IN_SERVICE,
    }
)
# Actions that run only after the same chat user confirms.
CONFIRM_INTENTS = frozenset({Intent.CANCEL, Intent.OUT_OF_SERVICE})
# What an LLM may answer with. Linking, confirming and declining are never left to a model.
LLM_INTENTS = frozenset({Intent.HELP} | READ_INTENTS | ACTION_INTENTS)


@dataclass
class ParsedIntent:
    intent: Intent
    printer: str | None = None
    file: str | None = None
    model: str | None = None  # "any <model>" target for queueing
    reason: str | None = None
    code: str | None = None
    source: str = "parser"  # "parser" | "llm"


_MENTION = re.compile(r"<@!?[A-Z0-9]+>|<#[A-Z0-9]+(\|[^>]*)?>", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_TRAILING = re.compile(r"[\s?!.,;:]+$")
_FILLER_PREFIX = re.compile(
    r"^(?:hey|hi|ok|okay|please|pls|can you|could you|would you|bot|farm|ophq)[\s,:]+", re.IGNORECASE
)
_ARTICLE = re.compile(r"^(?:the|my|our|printer)\s+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    text = _MENTION.sub(" ", text or "")
    text = text.replace("\u2019", "'")
    text = _SPACES.sub(" ", text).strip()
    prev = None
    while prev != text:
        prev = text
        text = _FILLER_PREFIX.sub("", text).strip()
    return _TRAILING.sub("", text)


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = _TRAILING.sub("", value.strip().strip("'\"`")).strip()
    prev = None
    while prev != value:
        prev = value
        value = _ARTICLE.sub("", value).strip()
    return value or None


_CONFIRM_WORDS = {"yes", "y", "yep", "yeah", "confirm", "confirmed", "do it", "go ahead", "ok", "okay", "sure"}
_DECLINE_WORDS = {"no", "n", "nope", "abort", "nevermind", "never mind", "cancel", "stop", "don't", "dont"}
_HELP_WORDS = {"help", "?", "commands", "what can you do", "usage"}

_RE_LINK = re.compile(r"^link\s+([A-Za-z0-9][A-Za-z0-9 -]{4,30})$", re.IGNORECASE)
_RE_PAUSE = re.compile(r"^(?:pause|hold)\s+(?:the\s+)?(?:print(?:ing)?\s+)?(?:on\s+)?(.+)$", re.IGNORECASE)
_RE_RESUME = re.compile(
    r"^(?:resume|unpause|continue)\s+(?:the\s+)?(?:print(?:ing)?\s+)?(?:on\s+)?(.+)$", re.IGNORECASE
)
_RE_CANCEL = re.compile(
    r"^(?:cancel|stop|abort|kill)\s+(?:the\s+)?(?:print(?:ing)?\s+|job\s+)?(?:on\s+)?(.+)$", re.IGNORECASE
)
_REASON = r"(?:\s*(?:because|for|reason|:|-|,)\s*(?P<reason>.+))?"
_RE_OOS = [
    re.compile(r"^(?:take|put|mark|set)\s+(?P<printer>.+?)\s+out\s+of\s+service" + _REASON + "$", re.IGNORECASE),
    re.compile(r"^out\s+of\s+service\s+(?P<printer>.+?)" + _REASON + "$", re.IGNORECASE),
    re.compile(r"^(?P<printer>.+?)\s+out\s+of\s+service" + _REASON + "$", re.IGNORECASE),
]
_RE_BACK = [
    re.compile(r"^(?:put|bring|set|mark)\s+(?P<printer>.+?)\s+back\s+(?:in|into)\s+service$", re.IGNORECASE),
    re.compile(r"^(?:put|bring|set|mark)\s+(?P<printer>.+?)\s+(?:in|into)\s+service$", re.IGNORECASE),
    re.compile(r"^return\s+(?P<printer>.+?)\s+to\s+service$", re.IGNORECASE),
    re.compile(r"^back\s+(?:in|into)\s+service\s+(?P<printer>.+)$", re.IGNORECASE),
    re.compile(r"^(?P<printer>.+?)\s+back\s+(?:in|into)\s+service$", re.IGNORECASE),
]
_RE_QUEUE_FILE = re.compile(r"^(?:queue|enqueue|print|send)\s+(?:up\s+)?(?P<rest>.+)$", re.IGNORECASE)
_RE_ON_TARGET = re.compile(r"^(?P<file>.+?)\s+(?:on|to|for)\s+(?P<target>.+)$", re.IGNORECASE)
_RE_ANY_MODEL = re.compile(r"^(?:any|an|a|whichever)\s+(?P<model>.+?)(?:\s+printer)?$", re.IGNORECASE)
_RE_READ_TARGET = re.compile(r"\b(?:on|for|of|at)\s+(?P<printer>[^?]+)$", re.IGNORECASE)

_QUEUE_LIST_WORDS = (
    "in the queue",
    "in queue",
    "queued",
    "waiting",
    "pending",
    "up next",
    "next up",
    "backlog",
    "queue list",
    "show queue",
    "list queue",
    "show the queue",
    "the queue",
)
_NOT_A_FILE = {"status", "progress", "list", "queue", "jobs", "eta", "help"}
_QUEUE_LIST_EXACT = {"queue", "queue status", "jobs"}
_FAILURE_WORDS = ("fail", "went wrong", "broke", "errored", "crash")
_ALARM_WORDS = ("alarm", "alert", "hms", "warning", "problem", "issue", "attention")
_STATUS_WORDS = (
    "status",
    "printing",
    "progress",
    "eta",
    "how long",
    "when will",
    "when is",
    "running",
    "doing",
    "busy",
    "idle",
    "farm",
    "printers",
    "what's on",
    "whats on",
    "done",
    "finish",
)


def _read_target(text: str) -> str | None:
    m = _RE_READ_TARGET.search(text)
    if m:
        target = _clean_name(m.group("printer"))
        if target and target.lower() not in ("the farm", "farm", "all", "everything", "now", "today"):
            return target
    m = re.match(r"^(?P<printer>.+?)\s+(?:status|progress|eta)$", text, re.IGNORECASE)
    if m:
        target = _clean_name(m.group("printer"))
        if target and target.lower() not in ("printer", "printers", "farm", "print", "what's the", "whats the"):
            return target
    m = re.match(r"^(?:status|progress|eta)\s+(?P<printer>.+)$", text, re.IGNORECASE)
    if m:
        return _clean_name(m.group("printer"))
    return None


def parse(text: str) -> ParsedIntent:
    """Map a chat message to one intent, without any model."""
    t = normalize_text(text)
    low = t.lower()
    if not low:
        return ParsedIntent(Intent.HELP)
    if low in _HELP_WORDS:
        return ParsedIntent(Intent.HELP)
    m = _RE_LINK.match(t)
    if m:
        return ParsedIntent(Intent.LINK, code=m.group(1))
    if low == "unlink":
        return ParsedIntent(Intent.UNLINK)
    if low in _CONFIRM_WORDS:
        return ParsedIntent(Intent.CONFIRM)
    if low in _DECLINE_WORDS:
        return ParsedIntent(Intent.DECLINE)
    if low in ("pause", "hold"):
        return ParsedIntent(Intent.PAUSE)
    if low in ("resume", "unpause", "continue"):
        return ParsedIntent(Intent.RESUME)

    for rx in _RE_BACK:
        m = rx.match(t)
        if m:
            return ParsedIntent(Intent.BACK_IN_SERVICE, printer=_clean_name(m.group("printer")))
    for rx in _RE_OOS:
        m = rx.match(t)
        if m:
            reason = m.groupdict().get("reason")
            return ParsedIntent(
                Intent.OUT_OF_SERVICE,
                printer=_clean_name(m.group("printer")),
                reason=reason.strip() if reason else None,
            )
    m = _RE_PAUSE.match(t)
    if m:
        return ParsedIntent(Intent.PAUSE, printer=_clean_name(m.group(1)))
    m = _RE_RESUME.match(t)
    if m:
        return ParsedIntent(Intent.RESUME, printer=_clean_name(m.group(1)))

    # Reads before "queue <file>" so "queue status" or "what is waiting" stay reads.
    is_question = "?" in (text or "") or low.startswith(("what", "which", "who", "how", "when", "is ", "are ", "any "))
    if low in _QUEUE_LIST_EXACT or any(w in low for w in _QUEUE_LIST_WORDS):
        return ParsedIntent(Intent.QUEUE_LIST, printer=_read_target(t))

    m = _RE_CANCEL.match(t)
    if m and not is_question:
        return ParsedIntent(Intent.CANCEL, printer=_clean_name(m.group(1)))

    m = _RE_QUEUE_FILE.match(t)
    if m and not is_question and _norm(m.group("rest")) not in _NOT_A_FILE:
        rest = m.group("rest").strip()
        file, printer, model = rest, None, None
        on = _RE_ON_TARGET.match(rest)
        if on:
            file = on.group("file")
            target = on.group("target").strip()
            anym = _RE_ANY_MODEL.match(target)
            if anym:
                model = _clean_name(anym.group("model"))
            else:
                printer = _clean_name(target)
        return ParsedIntent(Intent.QUEUE_FILE, file=_clean_name(file), printer=printer, model=model)

    if any(w in low for w in _FAILURE_WORDS):
        return ParsedIntent(Intent.FAILURES, printer=_read_target(t))
    if any(w in low for w in _ALARM_WORDS):
        return ParsedIntent(Intent.ALARMS, printer=_read_target(t))
    if any(w in low for w in _STATUS_WORDS):
        return ParsedIntent(Intent.STATUS, printer=_read_target(t))
    return ParsedIntent(Intent.UNKNOWN)


# --- Name matching -----------------------------------------------------------------


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _strip_ext(value: str) -> str:
    v = value or ""
    for ext in (".gcode.3mf", ".3mf", ".gcode", ".stl", ".step", ".stp", ".obj"):
        if v.lower().endswith(ext):
            return v[: -len(ext)]
    return v


@dataclass
class Match:
    item: dict | None
    candidates: list[dict]

    @property
    def ambiguous(self) -> bool:
        return self.item is None and len(self.candidates) > 1


def best_match(query: str | None, items: list[dict], keys: tuple[str, ...]) -> Match:
    """Find the one item ``query`` names. Exact, then substring, then fuzzy.

    Returns ``Match(item, [])`` on a single hit, ``Match(None, several)`` when the
    name fits more than one item, and ``Match(None, [])`` when it fits none.
    ``#<id>`` selects by id.
    """
    if not query:
        return Match(None, [])
    q = query.strip()
    if re.fullmatch(r"#?\d+", q):
        wanted = int(q.lstrip("#"))
        for it in items:
            if it.get("id") == wanted:
                return Match(it, [])
        if q.startswith("#"):
            return Match(None, [])
    nq = _norm(_strip_ext(q))
    if not nq:
        return Match(None, [])

    def names(it: dict) -> list[str]:
        return [_norm(_strip_ext(str(it.get(k) or ""))) for k in keys if it.get(k)]

    exact = [it for it in items if nq in names(it)]
    if len(exact) == 1:
        return Match(exact[0], [])
    if len(exact) > 1:
        return Match(None, exact)
    contains = [it for it in items if any(nq in n for n in names(it))]
    if len(contains) == 1:
        return Match(contains[0], [])
    if len(contains) > 1:
        return Match(None, contains)
    scored: list[tuple[float, dict]] = []
    for it in items:
        best = max((difflib.SequenceMatcher(None, nq, n).ratio() for n in names(it)), default=0.0)
        if best >= 0.75:
            scored.append((best, it))
    scored.sort(key=lambda s: s[0], reverse=True)
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.1):
        return Match(scored[0][1], [])
    return Match(None, [s[1] for s in scored])
