"""A Bambu Lab printer's snapshot comes from the farm's go2rtc when it is configured, so snapshots do not open
camera connections of their own next to the live view."""

from types import SimpleNamespace

import httpx
import pytest

from backend.app.api.routes import camera
from backend.app.core.config import settings

JPEG = b"\xff\xd8\xff\xe0frame"


def _printer(kind="bambu", pid=7):
    return SimpleNamespace(id=pid, connection_type=kind)


@pytest.fixture
def go2rtc(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("src") == "p7":
            return httpx.Response(200, content=JPEG)
        return httpx.Response(500, text="streams: not found")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "go2rtc_url", "http://openprinthq-go2rtc:1984/")
    return seen


async def test_a_bambu_printer_takes_its_frame_from_go2rtc(go2rtc):
    assert await camera._go2rtc_frame(_printer()) == JPEG
    assert go2rtc == ["http://openprinthq-go2rtc:1984/api/frame.jpeg?src=p7"]


async def test_no_frame_there_falls_back_to_a_direct_capture(go2rtc):
    assert await camera._go2rtc_frame(_printer(pid=8)) is None


async def test_other_printers_and_an_unset_url_are_left_alone(go2rtc, monkeypatch):
    assert await camera._go2rtc_frame(_printer(kind="octoprint")) is None
    monkeypatch.setattr(settings, "go2rtc_url", "")
    assert await camera._go2rtc_frame(_printer()) is None
    assert go2rtc == []


async def test_go2rtc_down_is_not_an_error(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(down), **kw))
    monkeypatch.setattr(settings, "go2rtc_url", "http://openprinthq-go2rtc:1984")
    assert await camera._go2rtc_frame(_printer()) is None
