"""Snapshot capture must stop hammering an endpoint that is reliably down.

These drive `_capture_snapshot` through a fake aiohttp session and assert on
whether a network attempt was made at all — the traffic, not just the logging,
is what the backoff is there to remove.
"""

import aiohttp
import pytest

from backend.app.services import external_camera
from backend.app.services.external_camera import _capture_snapshot, _snapshot_backoff

URL = "http://camera.invalid/snapshot.jpg"
JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _CountingSession:
    """Records how many GETs actually went out."""

    calls = 0

    def __init__(self, status=502, body=b"", raise_exc=None):
        self._status = status
        self._body = body
        self._raise_exc = raise_exc

    def get(self, _url):
        type(self).calls += 1
        if self._raise_exc:
            raise self._raise_exc
        return _FakeResponse(self._status, self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    _snapshot_backoff.reset()
    _CountingSession.calls = 0
    yield
    _snapshot_backoff.reset()


def _install(monkeypatch, **kw):
    monkeypatch.setattr(
        external_camera.aiohttp,
        "ClientSession",
        lambda *a, **k: _CountingSession(**kw),
    )


async def test_repeated_failures_stop_producing_requests(monkeypatch):
    """This is the powered-off-printer case: polling must go quiet."""
    _install(monkeypatch, status=502)

    for _ in range(8):
        assert await _capture_snapshot(URL, timeout=5) is None

    # First few attempts hit the network; once the cooldown opens, subsequent
    # polls inside it are skipped entirely rather than retried.
    assert _CountingSession.calls < 8
    assert _CountingSession.calls >= 2


async def test_a_single_failure_does_not_block_the_next_attempt(monkeypatch):
    """A blip must not cost the user a cooldown."""
    _install(monkeypatch, status=502)
    await _capture_snapshot(URL, timeout=5)
    assert _snapshot_backoff.should_attempt(URL) is True


async def test_force_bypasses_the_cooldown(monkeypatch):
    """A person clicking 'show me the camera' always gets a real attempt."""
    _install(monkeypatch, status=502)
    for _ in range(8):
        await _capture_snapshot(URL, timeout=5)
    before = _CountingSession.calls

    await _capture_snapshot(URL, timeout=5, force=True)
    assert _CountingSession.calls == before + 1


async def test_success_clears_the_backoff(monkeypatch):
    _install(monkeypatch, status=502)
    for _ in range(8):
        await _capture_snapshot(URL, timeout=5)
    assert _snapshot_backoff.should_attempt(URL) is False

    _install(monkeypatch, status=200, body=JPEG)
    assert await _capture_snapshot(URL, timeout=5, force=True) == JPEG
    assert _snapshot_backoff.should_attempt(URL) is True


async def test_transport_errors_count_toward_the_backoff(monkeypatch):
    """An unreachable host raises rather than returning a status; same treatment."""
    _install(monkeypatch, raise_exc=aiohttp.ClientError("no route to host"))
    for _ in range(8):
        assert await _capture_snapshot(URL, timeout=5) is None
    assert _snapshot_backoff.should_attempt(URL) is False


async def test_invalid_url_is_rejected_without_touching_the_network(monkeypatch):
    _install(monkeypatch, status=200, body=JPEG)
    assert await _capture_snapshot("not-a-url", timeout=5) is None
    assert _CountingSession.calls == 0
