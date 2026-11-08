"""Chat agent: deterministic intent parser, name matching and LLM answer validation."""

from __future__ import annotations

import pytest

from backend.app.services.chat_agent import llm
from backend.app.services.chat_agent.intents import Intent, best_match, parse


@pytest.mark.parametrize(
    ("text", "intent", "printer"),
    [
        ("help", Intent.HELP, None),
        ("", Intent.HELP, None),
        ("what's printing?", Intent.STATUS, None),
        ("status", Intent.STATUS, None),
        ("status of the X1C-1", Intent.STATUS, "X1C-1"),
        ("P1S-2 status", Intent.STATUS, "P1S-2"),
        ("how long until it is done", Intent.STATUS, None),
        ("what is waiting in the queue", Intent.QUEUE_LIST, None),
        ("queue", Intent.QUEUE_LIST, None),
        ("what's waiting on the voron", Intent.QUEUE_LIST, "voron"),
        ("what failed recently and why?", Intent.FAILURES, None),
        ("any failures on X1C-1", Intent.FAILURES, "X1C-1"),
        ("any alarms?", Intent.ALARMS, None),
        ("pause X1C-1", Intent.PAUSE, "X1C-1"),
        ("please pause the print on the X1C-1", Intent.PAUSE, "X1C-1"),
        ("pause", Intent.PAUSE, None),
        ("resume p1s 2", Intent.RESUME, "p1s 2"),
        ("cancel the print on X1C-1", Intent.CANCEL, "X1C-1"),
        ("stop voron", Intent.CANCEL, "voron"),
        ("put X1C-1 back in service", Intent.BACK_IN_SERVICE, "X1C-1"),
        ("return voron to service", Intent.BACK_IN_SERVICE, "voron"),
        ("yes", Intent.CONFIRM, None),
        ("Confirm", Intent.CONFIRM, None),
        ("no", Intent.DECLINE, None),
        ("cancel", Intent.DECLINE, None),
        ("unlink", Intent.UNLINK, None),
        ("make me a sandwich", Intent.UNKNOWN, None),
    ],
)
def test_parse(text, intent, printer):
    p = parse(text)
    assert p.intent == intent, (text, p)
    assert p.printer == printer, (text, p)


def test_parse_strips_mentions():
    p = parse("<@U0BOT> pause X1C-1")
    assert (p.intent, p.printer) == (Intent.PAUSE, "X1C-1")


def test_out_of_service_with_reason():
    p = parse("take X1C-1 out of service because nozzle clog")
    assert (p.intent, p.printer, p.reason) == (Intent.OUT_OF_SERVICE, "X1C-1", "nozzle clog")
    p = parse("mark the voron out of service")
    assert (p.intent, p.printer, p.reason) == (Intent.OUT_OF_SERVICE, "voron", None)


def test_queue_file():
    p = parse("queue benchy on X1C-1")
    assert (p.intent, p.file, p.printer, p.model) == (Intent.QUEUE_FILE, "benchy", "X1C-1", None)
    p = parse("print 'gear box.3mf' on any P1S")
    assert (p.intent, p.file, p.printer, p.model) == (Intent.QUEUE_FILE, "gear box.3mf", None, "P1S")
    p = parse("queue benchy")
    assert (p.intent, p.file, p.printer) == (Intent.QUEUE_FILE, "benchy", None)
    # "print status" is a question, not a file called "status"
    assert parse("print status").intent == Intent.STATUS


def test_link():
    p = parse("link abcd-efgh")
    assert (p.intent, p.code) == (Intent.LINK, "abcd-efgh")


PRINTERS = [
    {"id": 1, "name": "X1C-1"},
    {"id": 2, "name": "X1C-2"},
    {"id": 3, "name": "Voron 2.4"},
]


def test_best_match():
    assert best_match("x1c 1", PRINTERS, ("name",)).item["id"] == 1
    assert best_match("voron", PRINTERS, ("name",)).item["id"] == 3
    assert best_match("#2", PRINTERS, ("name",)).item["id"] == 2
    amb = best_match("x1c", PRINTERS, ("name",))
    assert amb.item is None and amb.ambiguous and {c["id"] for c in amb.candidates} == {1, 2}
    assert best_match("prusa", PRINTERS, ("name",)).item is None
    assert not best_match("prusa", PRINTERS, ("name",)).ambiguous
    # typo still finds the one close name
    assert best_match("vorn 2.4", PRINTERS, ("name",)).item["id"] == 3


def test_best_match_files_ignores_extension():
    files = [{"id": 7, "filename": "Benchy.gcode.3mf", "print_name": "3DBenchy"}, {"id": 8, "filename": "gear.3mf"}]
    assert best_match("benchy", files, ("filename", "print_name")).item["id"] == 7
    assert best_match("gear.3mf", files, ("filename", "print_name")).item["id"] == 8


class TestLlmAnswer:
    def test_valid(self):
        p = llm.interpret_response('{"intent": "pause", "printer": "X1C-1", "file": null}')
        assert (p.intent, p.printer, p.file, p.source) == (Intent.PAUSE, "X1C-1", None, "llm")

    def test_json_inside_text(self):
        p = llm.interpret_response('Sure: {"intent": "status", "printer": "null"}')
        assert (p.intent, p.printer) == (Intent.STATUS, None)

    @pytest.mark.parametrize(
        "content",
        [
            '{"intent": "link", "printer": null}',
            '{"intent": "confirm"}',
            '{"intent": "unlink"}',
            '{"intent": "delete_printer", "printer": "X1C-1"}',
            '{"intent": "shell", "command": "rm -rf /"}',
            "not json at all",
            "[1, 2]",
        ],
    )
    def test_outside_fixed_set_is_unknown(self, content):
        assert llm.interpret_response(content).intent == Intent.UNKNOWN

    async def test_classify_posts_openai_shape(self):
        import httpx

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": '{"intent": "resume", "printer": "p1"}'}}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = await llm.classify(
                "get p1 going again", base_url="http://llm.test/v1/", model="m", api_key="k", client=client
            )
        assert (p.intent, p.printer) == (Intent.RESUME, "p1")
        assert seen["url"] == "http://llm.test/v1/chat/completions"
        assert seen["auth"] == "Bearer k"
        assert seen["body"]["model"] == "m" and seen["body"]["temperature"] == 0

    async def test_classify_failure_is_unknown(self):
        import httpx

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            p = await llm.classify("x", base_url="http://llm.test/v1", model="m", client=client)
        assert p.intent == Intent.UNKNOWN
