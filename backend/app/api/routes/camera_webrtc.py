"""WebRTC (WHEP) signalling relay for printer cameras (#56).

See ``services/webrtc_camera.py`` for why signalling goes through the engine.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.app.core import database
from backend.app.core.auth import RequireCameraStreamTokenIfAuthEnabled
from backend.app.services.webrtc_camera import MAX_SDP_BYTES, WhepError, negotiate_whep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printers", tags=["camera"])


@router.post(
    "/{printer_id}/camera/webrtc",
    status_code=201,
    response_class=Response,
    responses={201: {"content": {"application/sdp": {}}, "description": "SDP answer"}},
)
async def camera_webrtc_offer(
    printer_id: int,
    request: Request,
    _: None = RequireCameraStreamTokenIfAuthEnabled,
):
    """Relay a browser's WHEP SDP offer to the printer's WebRTC camera.

    Send the offer as the raw request body (``Content-Type: application/sdp``);
    the SDP answer comes back the same way. Authenticated like the MJPEG stream
    (``?token=`` camera stream token when auth is enabled), so a Cam Wall kiosk
    can use it too. Returns 404 when the printer has no WebRTC URL, 502/504 when
    the WebRTC server fails; the player then falls back to the MJPEG stream.
    """
    from backend.app.api.routes.camera import get_printer_or_404

    body = await request.body()
    if len(body) > MAX_SDP_BYTES:
        raise HTTPException(status_code=413, detail="SDP offer too large")

    async with database.async_session() as db:
        printer = await get_printer_or_404(printer_id, db)
    whep_url = printer.camera_webrtc_url
    if not whep_url:
        raise HTTPException(status_code=404, detail="No WebRTC camera configured for this printer")

    try:
        answer = await negotiate_whep(whep_url, body.decode("utf-8", errors="replace"))
    except WhepError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e

    return Response(
        content=answer,
        status_code=201,
        media_type="application/sdp",
        headers={"Cache-Control": "no-store"},
    )
