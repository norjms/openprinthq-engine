"""Optional language model for free-form phrasing.

Only used when the deterministic parser (intents.parse) does not recognise a message
and an OpenAI-compatible endpoint is configured (for example a local Ollama at
``http://<host>:11434/v1``). The model is asked to classify the message into one of
``intents.LLM_INTENTS`` and to extract raw names. It never calls the engine: its answer
becomes a ``ParsedIntent`` that goes through the same name resolution, permission
checks and confirmation step as a parsed message. Anything outside the fixed set, or
an answer that is not valid JSON, is treated as "not understood".
"""

from __future__ import annotations

import json
import logging

import httpx

from backend.app.services.chat_agent.intents import LLM_INTENTS, Intent, ParsedIntent

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20.0

_SYSTEM = (
    "You turn a message sent to a 3D print farm chat bot into JSON. "
    "Reply with one JSON object and nothing else, with these keys: "
    '"intent" (one of: {intents}), "printer" (printer name or null), '
    '"file" (library file name or null), "model" (printer model when the user says any printer of a model, '
    'else null), "reason" (free text reason for taking a printer out of service, else null). '
    "Meanings: status = what is printing, progress, time left; failures = recent failed prints and why; "
    "queue_list = what is waiting in the queue; alarms = printer alarms and errors; "
    "queue_file = add a library file to the print queue; pause / resume / cancel = the current print on a printer; "
    "out_of_service / back_in_service = stop or restart giving a printer new jobs; help = anything else about "
    'the bot. If the message is none of these, use "unknown". Never invent names that are not in the message.'
)


def _intent_names() -> list[str]:
    return sorted(i.value for i in LLM_INTENTS) + [Intent.UNKNOWN.value]


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in ("null", "none"):
        return None
    return value[:200]


def interpret_response(content: str) -> ParsedIntent:
    """Validate a model answer. Anything unexpected becomes UNKNOWN."""
    try:
        start, end = content.find("{"), content.rfind("}")
        data = json.loads(content[start : end + 1]) if start >= 0 and end > start else {}
    except (ValueError, TypeError):
        return ParsedIntent(Intent.UNKNOWN, source="llm")
    if not isinstance(data, dict):
        return ParsedIntent(Intent.UNKNOWN, source="llm")
    try:
        intent = Intent(str(data.get("intent", "")).strip().lower())
    except ValueError:
        return ParsedIntent(Intent.UNKNOWN, source="llm")
    if intent not in LLM_INTENTS:
        return ParsedIntent(Intent.UNKNOWN, source="llm")
    return ParsedIntent(
        intent,
        printer=_str_or_none(data.get("printer")),
        file=_str_or_none(data.get("file")),
        model=_str_or_none(data.get("model")),
        reason=_str_or_none(data.get("reason")),
        source="llm",
    )


async def classify(text: str, *, base_url: str, model: str, api_key: str = "", client=None) -> ParsedIntent:
    """Ask the configured model which intent ``text`` is. Failures become UNKNOWN."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM.format(intents=", ".join(_intent_names()))},
            {"role": "user", "content": text[:1000]},
        ],
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("chat agent: language model answered HTTP %s", resp.status_code)
            return ParsedIntent(Intent.UNKNOWN, source="llm")
        content = resp.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001 - the model is optional; failing it means "not understood"
        logger.warning("chat agent: language model call failed: %s", type(e).__name__)
        return ParsedIntent(Intent.UNKNOWN, source="llm")
    finally:
        if own:
            await client.aclose()
    return interpret_response(content)
