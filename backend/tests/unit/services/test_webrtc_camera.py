"""Unit tests for WebRTC (WHEP) camera helpers (#56)."""

from __future__ import annotations

import pytest

from backend.app.services import webrtc_camera
from backend.app.services.webrtc_camera import (
    WhepError,
    apply_go2rtc_fields,
    derive_go2rtc_urls,
    negotiate_whep,
    sanitize_whep_url,
)

OFFER = "v=0\r\no=- 1 2 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"


class TestDeriveGo2rtcUrls:
    def test_builds_all_three_urls(self):
        urls = derive_go2rtc_urls("http://go2rtc.lan:1984", "voron")
        assert urls == {
            "camera_webrtc_url": "http://go2rtc.lan:1984/api/webrtc?src=voron",
            "external_camera_url": "http://go2rtc.lan:1984/api/stream.mjpeg?src=voron",
            "external_camera_snapshot_url": "http://go2rtc.lan:1984/api/frame.jpeg?src=voron",
        }

    def test_trailing_slash_and_path_prefix(self):
        urls = derive_go2rtc_urls("https://cams.example.com/go2rtc/", "h2d")
        assert urls["camera_webrtc_url"] == "https://cams.example.com/go2rtc/api/webrtc?src=h2d"

    def test_stream_name_is_url_encoded(self):
        urls = derive_go2rtc_urls("http://10.0.0.5:1984", "bay 1&x=y")
        assert urls["camera_webrtc_url"].endswith("?src=bay%201%26x%3Dy")

    @pytest.mark.parametrize(
        "base",
        [
            "ftp://go2rtc.lan:1984",
            "http://169.254.169.254",
            "http://localhost:1984",
            "http://127.0.0.1:1984",
            "http://metadata.google.internal",
            "not a url",
            "",
        ],
    )
    def test_rejects_disallowed_bases(self, base):
        with pytest.raises(ValueError):
            derive_go2rtc_urls(base, "cam")

    def test_rejects_base_with_query(self):
        with pytest.raises(ValueError):
            derive_go2rtc_urls("http://go2rtc.lan:1984/api/webrtc?src=a", "cam")

    def test_rejects_empty_stream(self):
        with pytest.raises(ValueError):
            derive_go2rtc_urls("http://go2rtc.lan:1984", "  ")


class TestApplyGo2rtcFields:
    def test_no_go2rtc_fields_is_a_no_op(self):
        data = {"name": "x", "go2rtc_url": None, "go2rtc_stream": None}
        assert apply_go2rtc_fields(data) == {"name": "x"}

    def test_fills_camera_columns_and_enables(self):
        data = apply_go2rtc_fields({"go2rtc_url": "http://go2rtc.lan:1984", "go2rtc_stream": "cam"})
        assert data["external_camera_enabled"] is True
        assert data["external_camera_type"] == "mjpeg"
        assert data["external_camera_url"].endswith("/api/stream.mjpeg?src=cam")
        assert data["external_camera_snapshot_url"].endswith("/api/frame.jpeg?src=cam")
        assert data["camera_webrtc_url"].endswith("/api/webrtc?src=cam")
        assert "go2rtc_url" not in data and "go2rtc_stream" not in data

    def test_explicit_values_win(self):
        data = apply_go2rtc_fields(
            {
                "go2rtc_url": "http://go2rtc.lan:1984",
                "go2rtc_stream": "cam",
                "external_camera_snapshot_url": "http://cam.lan/snap.jpg",
                "external_camera_type": "rtsp",
            }
        )
        assert data["external_camera_snapshot_url"] == "http://cam.lan/snap.jpg"
        assert data["external_camera_type"] == "rtsp"

    def test_half_a_pair_is_rejected(self):
        with pytest.raises(ValueError):
            apply_go2rtc_fields({"go2rtc_url": "http://go2rtc.lan:1984"})


class TestSanitizeWhepUrl:
    def test_accepts_http_and_https(self):
        assert sanitize_whep_url("http://go2rtc.lan:1984/api/webrtc?src=a") == "http://go2rtc.lan:1984/api/webrtc?src=a"
        assert sanitize_whep_url("https://mtx.lan:8889/cam/whep") == "https://mtx.lan:8889/cam/whep"

    @pytest.mark.parametrize(
        "url",
        ["rtsp://cam.lan/live", "file:///etc/passwd", "http://169.254.169.254/latest", "http://localhost/whep", ""],
    )
    def test_rejects(self, url):
        assert sanitize_whep_url(url) is None


class _FakeContent:
    def __init__(self, body: bytes):
        self._body = body

    async def read(self, n: int) -> bytes:
        return self._body[:n]


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for aiohttp.ClientSession and records the one POST it gets."""

    calls: list[dict] = []
    status = 201
    body = ANSWER.encode()
    raise_exc: BaseException | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None, allow_redirects=True):
        type(self).calls.append({"url": url, "data": data, "headers": headers, "allow_redirects": allow_redirects})
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        return _FakeResponse(type(self).status, type(self).body)


@pytest.fixture
def fake_session(monkeypatch):
    _FakeSession.calls = []
    _FakeSession.status = 201
    _FakeSession.body = ANSWER.encode()
    _FakeSession.raise_exc = None
    monkeypatch.setattr(webrtc_camera.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession


@pytest.mark.asyncio
class TestNegotiateWhep:
    async def test_posts_offer_as_sdp_and_returns_answer(self, fake_session):
        answer = await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert answer == ANSWER
        call = fake_session.calls[0]
        assert call["url"] == "http://go2rtc.lan:1984/api/webrtc?src=cam"
        assert call["headers"]["Content-Type"] == "application/sdp"
        assert call["data"] == OFFER.encode()
        assert call["allow_redirects"] is False

    async def test_accepts_200(self, fake_session):
        fake_session.status = 200
        assert await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER) == ANSWER

    async def test_blocked_url_never_leaves_the_engine(self, fake_session):
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://169.254.169.254/latest/meta-data", OFFER)
        assert exc.value.status == 400
        assert fake_session.calls == []

    async def test_rejects_non_sdp_body(self, fake_session):
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", "hello")
        assert exc.value.status == 400
        assert fake_session.calls == []

    async def test_server_error_is_502(self, fake_session):
        fake_session.status = 500
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert exc.value.status == 502

    async def test_redirect_is_not_followed(self, fake_session):
        fake_session.status = 307
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert exc.value.status == 502

    async def test_non_sdp_answer_is_502(self, fake_session):
        fake_session.body = b'{"error": "stream not found"}'
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert exc.value.status == 502

    async def test_unreachable_is_502(self, fake_session):
        import aiohttp

        fake_session.raise_exc = aiohttp.ClientConnectionError("refused")
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert exc.value.status == 502

    async def test_timeout_is_504(self, fake_session):
        fake_session.raise_exc = TimeoutError()
        with pytest.raises(WhepError) as exc:
            await negotiate_whep("http://go2rtc.lan:1984/api/webrtc?src=cam", OFFER)
        assert exc.value.status == 504
