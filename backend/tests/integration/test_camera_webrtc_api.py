"""Integration tests for WebRTC (WHEP) camera config and signalling (#56)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

OFFER = "v=0\r\no=- 1 2 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
WHEP = "http://go2rtc.lan:1984/api/webrtc?src=cam"


class TestWebrtcConfig:
    async def test_patch_stores_and_returns_url_on_detail_and_list(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(name="WebRTC P1")
        r = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"camera_webrtc_url": WHEP})
        assert r.status_code == 200, r.text
        assert r.json()["camera_webrtc_url"] == WHEP

        detail = await async_client.get(f"/api/v1/printers/{printer.id}")
        assert detail.json()["camera_webrtc_url"] == WHEP
        listing = await async_client.get("/api/v1/printers/")
        row = next(p for p in listing.json() if p["id"] == printer.id)
        assert row["camera_webrtc_url"] == WHEP

    async def test_blank_clears_the_url(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(camera_webrtc_url=WHEP)
        r = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"camera_webrtc_url": ""})
        assert r.status_code == 200, r.text
        assert r.json()["camera_webrtc_url"] is None

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "rtsp://cam.lan/live",
            "http://169.254.169.254/latest/meta-data",
            "http://localhost/whep",
        ],
    )
    async def test_disallowed_urls_are_rejected(self, async_client: AsyncClient, printer_factory, url):
        printer = await printer_factory()
        r = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"camera_webrtc_url": url})
        assert r.status_code == 422

    async def test_go2rtc_pair_derives_all_camera_urls(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(external_camera_type="rtsp")
        r = await async_client.patch(
            f"/api/v1/printers/{printer.id}",
            json={"go2rtc_url": "http://go2rtc.lan:1984", "go2rtc_stream": "cam"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["camera_webrtc_url"] == WHEP
        assert body["external_camera_url"] == "http://go2rtc.lan:1984/api/stream.mjpeg?src=cam"
        assert body["external_camera_snapshot_url"] == "http://go2rtc.lan:1984/api/frame.jpeg?src=cam"
        assert body["external_camera_type"] == "mjpeg"
        assert body["external_camera_enabled"] is True
        assert "go2rtc_url" not in body and "go2rtc_stream" not in body

    async def test_half_a_go2rtc_pair_is_rejected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        r = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"go2rtc_stream": "cam"})
        assert r.status_code == 422

    async def test_go2rtc_base_on_blocked_host_is_rejected(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        r = await async_client.patch(
            f"/api/v1/printers/{printer.id}",
            json={"go2rtc_url": "http://169.254.169.254", "go2rtc_stream": "cam"},
        )
        assert r.status_code == 422

    async def test_create_external_printer_with_go2rtc(self, async_client: AsyncClient, monkeypatch):
        from backend.app.services.printer_manager import printer_manager

        async def _no_connect(*_args, **_kwargs):
            return True

        monkeypatch.setattr(printer_manager, "connect_printer", _no_connect)
        r = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "Voron",
                "connection_type": "klipper",
                "ip_address": "192.168.1.50",
                "go2rtc_url": "http://go2rtc.lan:1984",
                "go2rtc_stream": "cam",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["camera_webrtc_url"] == WHEP
        assert body["external_camera_enabled"] is True
        assert body["external_camera_snapshot_url"].endswith("/api/frame.jpeg?src=cam")


class TestWebrtcSignalling:
    async def test_no_webrtc_url_is_404(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/camera/webrtc",
            content=OFFER,
            headers={"Content-Type": "application/sdp"},
        )
        assert r.status_code == 404

    async def test_unknown_printer_is_404(self, async_client: AsyncClient):
        r = await async_client.post(
            "/api/v1/printers/999999/camera/webrtc", content=OFFER, headers={"Content-Type": "application/sdp"}
        )
        assert r.status_code == 404

    async def test_relays_offer_and_returns_answer(self, async_client: AsyncClient, printer_factory, monkeypatch):
        from backend.app.api.routes import camera_webrtc

        seen = {}

        async def fake_negotiate(url, offer):
            seen["url"] = url
            seen["offer"] = offer
            return ANSWER

        monkeypatch.setattr(camera_webrtc, "negotiate_whep", fake_negotiate)
        printer = await printer_factory(camera_webrtc_url=WHEP)
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/camera/webrtc",
            content=OFFER,
            headers={"Content-Type": "application/sdp"},
        )
        assert r.status_code == 201, r.text
        assert r.headers["content-type"].startswith("application/sdp")
        assert r.text == ANSWER
        assert seen == {"url": WHEP, "offer": OFFER}

    async def test_whep_failure_maps_to_status(self, async_client: AsyncClient, printer_factory, monkeypatch):
        from backend.app.api.routes import camera_webrtc
        from backend.app.services.webrtc_camera import WhepError

        async def failing(url, offer):
            raise WhepError(502, "WebRTC server unreachable: ClientConnectorError")

        monkeypatch.setattr(camera_webrtc, "negotiate_whep", failing)
        printer = await printer_factory(camera_webrtc_url=WHEP)
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/camera/webrtc",
            content=OFFER,
            headers={"Content-Type": "application/sdp"},
        )
        assert r.status_code == 502

    async def test_oversized_offer_is_413(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory(camera_webrtc_url=WHEP)
        r = await async_client.post(
            f"/api/v1/printers/{printer.id}/camera/webrtc",
            content="v=0\r\n" + "a" * (70 * 1024),
            headers={"Content-Type": "application/sdp"},
        )
        assert r.status_code == 413

    async def test_auth_enabled_requires_stream_token(self, async_client: AsyncClient, printer_factory, monkeypatch):
        from backend.app.api.routes import camera_webrtc

        async def fake_negotiate(url, offer):
            return ANSWER

        monkeypatch.setattr(camera_webrtc, "negotiate_whep", fake_negotiate)
        printer = await printer_factory(camera_webrtc_url=WHEP)

        await async_client.post(
            "/api/v1/auth/setup",
            json={"auth_enabled": True, "admin_username": "webrtcadmin", "admin_password": "AdminPass1!"},
        )
        login = await async_client.post(
            "/api/v1/auth/login", json={"username": "webrtcadmin", "password": "AdminPass1!"}
        )
        jwt = login.json()["access_token"]

        url = f"/api/v1/printers/{printer.id}/camera/webrtc"
        no_token = await async_client.post(url, content=OFFER, headers={"Content-Type": "application/sdp"})
        assert no_token.status_code == 401

        tok = await async_client.post(
            "/api/v1/printers/camera/stream-token", headers={"Authorization": f"Bearer {jwt}"}
        )
        token = tok.json()["token"]
        ok = await async_client.post(f"{url}?token={token}", content=OFFER, headers={"Content-Type": "application/sdp"})
        assert ok.status_code == 201, ok.text

    async def test_camwall_flags_webrtc_without_leaking_url(self, async_client: AsyncClient, printer_factory):
        await printer_factory(name="A with webrtc", camera_webrtc_url=WHEP)
        await printer_factory(name="B without")
        r = await async_client.get("/api/v1/camwall/printers")
        assert r.status_code == 200, r.text
        by_name = {p["name"]: p for p in r.json()}
        assert by_name["A with webrtc"]["webrtc"] is True
        assert by_name["B without"]["webrtc"] is False
        assert WHEP not in r.text
