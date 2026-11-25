"""WebRTC (WHEP) camera playback support (#56).

The engine cannot decode WebRTC, and it does not need to. A WHEP URL on a
printer is a *playback* option for browsers: the camera wall and the printer
camera views try WebRTC first and fall back to the engine's own MJPEG stream
(and from there to snapshots) when it does not connect. Every server-side
capture (finish photos, plate checks, notification thumbnails, timelapse,
Obico) keeps using the printer's ordinary camera source, so a WebRTC camera
never loses snapshot capture.

Signalling goes through the engine (``POST /printers/{id}/camera/webrtc``)
rather than straight from the browser to the WHEP server:

* the engine applies its normal camera auth, so the WHEP server never has to
  be exposed to anyone who can reach the engine UI;
* an engine served over HTTPS cannot ``fetch()`` a plain-HTTP go2rtc on the
  LAN (mixed content), and go2rtc does not send CORS headers by default;
* the WHEP URL (often an internal address) is never handed to a kiosk wall.

Only the SDP offer and answer (a few KB) cross the engine. Media flows
browser to WHEP server directly over ICE, exactly as with the earlier
control-plane go2rtc relay.

The proxy only ever talks to the URL stored on the printer by someone with
printer-update permission, and that URL passes the same validation as every
other camera URL (``external_camera._sanitize_camera_url``), so it is not an
open relay to arbitrary addresses.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlparse

import aiohttp

from backend.app.services.external_camera import _sanitize_camera_url

logger = logging.getLogger(__name__)

WHEP_SCHEMES = ("http", "https")
# SDP offers and answers are a few KB; anything far larger is not SDP.
MAX_SDP_BYTES = 64 * 1024
WHEP_TIMEOUT_SECONDS = 10


class WhepError(Exception):
    """Negotiation with the WHEP server failed; ``status`` is the HTTP code to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def sanitize_whep_url(url: str | None) -> str | None:
    """Validate a WHEP endpoint URL with the shared camera URL rules (http/https only)."""
    if not url:
        return None
    return _sanitize_camera_url(url.strip(), WHEP_SCHEMES)


def derive_go2rtc_urls(base_url: str, stream: str) -> dict[str, str]:
    """Build the WHEP, MJPEG and snapshot URLs for one go2rtc stream.

    ``base_url`` is the go2rtc API root (``http://host:1984``, or a reverse
    proxy path in front of it). Raises ``ValueError`` when the base is not a
    valid camera URL or the stream name is empty.
    """
    name = (stream or "").strip()
    if not name:
        raise ValueError("go2rtc stream name is required")
    base = (base_url or "").strip().rstrip("/")
    safe = _sanitize_camera_url(base, WHEP_SCHEMES)
    if not safe:
        raise ValueError("go2rtc URL must be an http(s) URL on an allowed host")
    parsed = urlparse(safe)
    if parsed.query or parsed.fragment:
        raise ValueError("go2rtc URL must be the API root, without a query string")
    root = safe.rstrip("/")
    src = quote(name, safe="")
    return {
        "camera_webrtc_url": f"{root}/api/webrtc?src={src}",
        "external_camera_url": f"{root}/api/stream.mjpeg?src={src}",
        "external_camera_snapshot_url": f"{root}/api/frame.jpeg?src={src}",
    }


def apply_go2rtc_fields(data: dict) -> dict:
    """Turn the write-only ``go2rtc_url`` / ``go2rtc_stream`` pair into camera columns.

    Mutates and returns ``data`` (a create/update payload dict). When both are
    given, the printer gets an enabled MJPEG external camera on the go2rtc
    stream, the go2rtc frame endpoint as its snapshot override (go2rtc's first
    MJPEG frame is often a stale keyframe) and the go2rtc WHEP endpoint for
    browser playback. Explicit values in the same payload win over derived ones.
    """
    base = data.pop("go2rtc_url", None)
    stream = data.pop("go2rtc_stream", None)
    if not base and not stream:
        return data
    if not base or not stream:
        raise ValueError("go2rtc_url and go2rtc_stream must be given together")
    derived = derive_go2rtc_urls(base, stream)
    for key, value in derived.items():
        if not data.get(key):
            data[key] = value
    if not data.get("external_camera_type"):
        data["external_camera_type"] = "mjpeg"
    data["external_camera_enabled"] = True
    return data


async def negotiate_whep(whep_url: str, offer_sdp: str, timeout: float = WHEP_TIMEOUT_SECONDS) -> str:
    """POST an SDP offer to a WHEP endpoint and return the SDP answer.

    Non-trickle: the browser gathers its ICE candidates before sending, so one
    request carries everything and no PATCH or DELETE follow-up is needed (the
    server tears the session down when ICE disconnects). Redirects are not
    followed, so a validated URL cannot bounce the request somewhere else.
    """
    safe = sanitize_whep_url(whep_url)
    if not safe:
        raise WhepError(400, "WebRTC URL is not a valid camera URL")
    if not offer_sdp or not offer_sdp.lstrip().startswith("v=0"):
        raise WhepError(400, "Body must be an SDP offer")
    if len(offer_sdp.encode()) > MAX_SDP_BYTES:
        raise WhepError(413, "SDP offer too large")

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.post(
                safe,
                data=offer_sdp.encode(),
                headers={"Content-Type": "application/sdp", "Accept": "application/sdp"},
                allow_redirects=False,
            ) as resp,
        ):
            body = await resp.content.read(MAX_SDP_BYTES + 1)
            if resp.status not in (200, 201):
                logger.info("WHEP server answered HTTP %s", resp.status)
                raise WhepError(502, f"WebRTC server answered HTTP {resp.status}")
    except WhepError:
        raise
    except TimeoutError as e:
        raise WhepError(504, "WebRTC server did not answer in time") from e
    except (aiohttp.ClientError, OSError) as e:
        logger.info("WHEP negotiation failed: %s", type(e).__name__)
        raise WhepError(502, f"WebRTC server unreachable: {type(e).__name__}") from e

    if len(body) > MAX_SDP_BYTES:
        raise WhepError(502, "WebRTC server answer too large")
    answer = body.decode("utf-8", errors="replace")
    if not answer.lstrip().startswith("v=0"):
        raise WhepError(502, "WebRTC server did not return an SDP answer")
    return answer
