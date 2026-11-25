# WebRTC (WHEP) cameras

A printer can have a **WebRTC URL**: a [WHEP](https://www.ietf.org/archive/id/draft-ietf-wish-whep-01.html)
endpoint such as go2rtc's `/api/webrtc?src=<stream>` or MediaMTX's `/<path>/whep`. The Cam Wall,
the printer camera window and the stand-alone camera page then play the camera over WebRTC, with
lower latency and far less bandwidth than MJPEG.

## How playback falls back

Each live view tries, in order:

1. **WebRTC.** While it negotiates, the Cam Wall tile shows a still frame. The video takes over
   only once the browser reports it is actually playing.
2. **MJPEG**, the engine's normal `/printers/{id}/camera/stream`, when WebRTC fails: the browser
   has no WebRTC, the WHEP server answers with an error or not at all, no frame plays within
   8 seconds, or a playing connection fails.
3. **Snapshots** (Cam Wall tiles), refreshed on the wall's snapshot interval, when the MJPEG
   stream fails too.

A failed WebRTC attempt is not retried until the view is reopened or Retry is pressed, so a
broken WHEP server never makes a view flap between sources.

## Snapshots keep working

The engine never decodes WebRTC. Finish photos, plate checks, notification thumbnails, layer
timelapse and Obico keep using the printer's regular camera source: the external camera (MJPEG,
RTSP, snapshot or USB, plus the optional snapshot URL) or the printer's built-in camera. That is
why the WebRTC URL is a separate field and not a fourth camera type: a camera the engine could
not read would have no finish photos.

It also means the WebRTC URL works with or without the external camera switch. With the switch
off, it can front the printer's own camera (go2rtc ingesting a Bambu RTSP feed, for example),
and the built-in stream is the fallback.

## Setting it up

Settings > Camera > External cameras has, for each printer:

- **WebRTC URL (optional)**: the WHEP endpoint. Saved when the field loses focus; empty clears it.
- **Set up from go2rtc**: a go2rtc address (`http://go2rtc.local:1984`, or the path a reverse
  proxy serves it on) and a stream name. Apply fills in all three URLs and turns the external
  camera on:

  | Field | Value |
  |---|---|
  | `external_camera_url` (type `mjpeg`) | `<go2rtc>/api/stream.mjpeg?src=<stream>` |
  | `external_camera_snapshot_url` | `<go2rtc>/api/frame.jpeg?src=<stream>` |
  | `camera_webrtc_url` | `<go2rtc>/api/webrtc?src=<stream>` |

  The snapshot override matters for go2rtc: its first MJPEG frame is often a stale keyframe.

The same through the API:

```
PATCH /api/v1/printers/{id}
{"camera_webrtc_url": "http://go2rtc.local:1984/api/webrtc?src=h2d"}

PATCH /api/v1/printers/{id}
{"go2rtc_url": "http://go2rtc.local:1984", "go2rtc_stream": "h2d"}
```

`go2rtc_url` and `go2rtc_stream` are write-only (also accepted by `POST /api/v1/printers/`), must be
given together, and values sent explicitly in the same request win over the derived ones.
`camera_webrtc_url` is returned on `GET /api/v1/printers` and `GET /api/v1/printers/{id}`. The
token-authenticated Cam Wall feed (`GET /api/v1/camwall/printers`) returns only a `webrtc` flag,
never the URL.

URLs are checked with the same rules as every other camera URL: `http` or `https`, and no cloud
metadata, loopback or link-local hosts. A rejected URL answers 422.

## Signalling goes through the engine

The browser does not talk to the WHEP server directly. It posts its SDP offer to the engine:

```
POST /api/v1/printers/{id}/camera/webrtc?token=<camera stream token>
Content-Type: application/sdp

<offer>
```

and gets `201` with the SDP answer (`application/sdp`). The engine forwards the offer to the
printer's stored WebRTC URL and returns the answer. Only the offer and the answer, a few KB, pass
through the engine; the video flows from the WHEP server to the browser directly.

Why the relay:

- **Auth.** The call needs the same camera stream token as the MJPEG stream (a Cam Wall token
  works too), so the WHEP server does not have to be reachable by, or open to, anyone who can
  load the UI.
- **Mixed content and CORS.** An engine served over HTTPS cannot `fetch()` a plain-HTTP go2rtc,
  and go2rtc sends no CORS headers by default.
- **No internal addresses on kiosk screens.** A Cam Wall URL on a TV never learns the WHEP URL.

It is not an open proxy. The engine only ever posts to the URL stored on the printer (set by
someone with printer-update permission and validated as above), does not follow redirects,
accepts at most 64 KB of SDP each way, and gives up after 10 seconds.

Errors: `404` when the printer has no WebRTC URL, `413` for an oversized offer, `502` when the
WHEP server is unreachable or answers with an error or something that is not SDP, `504` on a
timeout. Each one makes the browser fall back to MJPEG.

The exchange is non-trickle: the browser gathers its ICE candidates (for at most 2 seconds)
before sending the offer, so there is no PATCH or DELETE follow-up. The WHEP server ends the
session when the browser closes the connection.

## Reaching the video from outside the LAN

Signalling goes through the engine, so it works wherever the engine UI works. The media still has
to reach the browser over ICE. On the LAN that just works. From elsewhere, the WHEP server must
advertise an address the browser can reach (go2rtc: `webrtc.candidates`) or a TURN relay
(`webrtc.ice_servers`). The browser side uses no STUN or TURN server of its own, so no request
leaves for a third party. When the media cannot get through, the view falls back to MJPEG, which
the engine carries.

## Not covered

- The streaming overlay (`/overlay/{id}`) keeps MJPEG.
- Artisan Hub and the phone app show snapshots, which come from the engine's snapshot endpoint and
  are unaffected.
