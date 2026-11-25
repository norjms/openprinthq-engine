# Elegoo printers (SDCP and Centauri Carbon 2)

> **Untested on hardware.** OpenPrintHQ owns no Elegoo printer. Both clients
> are built from Elegoo's published protocol documents and SDK and from the
> captured payloads of the elegoo-homeassistant project (sources below); the
> payloads are the test fixtures in `backend/tests/_fixtures/elegoo/`. The
> printer card and the add dialog mark these connection types "Untested on
> hardware".

| Connection type | Printers | Transport |
|-----------------|----------|-----------|
| `elegoo` | Centauri Carbon (CC1) and other FDM printers speaking SDCP v3 | WebSocket `ws://<ip>:3030/websocket` |
| `elegoo_cc2` | Centauri Carbon 2 | MQTT broker on the printer, `<ip>:1883` |

Resin printers (Saturn, Mars) also speak SDCP but print `.ctb` files and are
out of scope (the printer support plan keeps resin out); an `elegoo` printer
here is expected to print `.gcode`.

## What works

| | `elegoo` (SDCP) | `elegoo_cc2` |
|---|---|---|
| Monitor (state, progress, layers, time left, nozzle, bed, chamber) | yes | yes |
| Pause, resume, stop | Cmd 129 / 131 / 130 | methods 1021 / 1023 / 1022 |
| Print a library `.gcode` | upload, then Cmd 128 | upload, then method 1020 |
| Temperatures | Cmd 403 `TempTargetNozzle` / `TempTargetHotbed` | method 1028 |
| Light | Cmd 403 `LightStatus.SecondLight` | method 1029 |
| Home, raw G-code | no (neither protocol has raw G-code; the home route sends G28) | no |
| CANVAS slots | Cmd 324, polled every 30 s | method 2005, polled every 30 s |
| CANVAS slot remap (#52) | `slot_map` in Cmd 128 | `config.slot_map` in method 1020 |

## Setting up

- **SDCP:** add the printer with its IP address. The engine finds the
  printer's MainboardID with a unicast discovery packet (`M99999` to UDP port
  3000) or from the first status message. If UDP cannot reach the printer, put
  the MainboardID in the serial field. The port field defaults to 3030.
- **Centauri Carbon 2:** turn on **LAN Only Mode** on the printer (cloud mode
  is not supported). Add it with its IP address and, only if you set one on
  the printer, the access code (the MQTT password; `123456` otherwise). The
  engine finds the serial with a discovery packet to UDP port 52700; put the
  serial in the serial field if UDP is blocked. The printer accepts about four
  MQTT clients at once (slicer, app, OpenPrintHQ), so a fifth is refused.

## How a job is sent

1. The library `.gcode` is uploaded:
   - SDCP: `POST http://<ip>:3030/uploadFile/upload`, multipart, 1 MB packets
     with `S-File-MD5`, `Check=1`, `Offset`, `Uuid` and `TotalSize`.
   - CC2: `PUT http://<ip>/upload` in 1 MB chunks with `Content-Range`,
     `X-File-Name`, `X-File-MD5` and `X-Token`, over one kept-alive connection
     and without retries (a new connection per chunk gets HTTP 429 and a
     retried chunk corrupts the file, measured by elegoo-homeassistant on
     firmware 02.01.00.00).
2. If the printer reports a CANVAS, the G-code's filaments are remapped to
   trays (same material, closest colour; see `docs/multi-material-units.md`)
   and stored on the queue item as `ams_mapping`.
3. The start command carries the remap as `slot_map`: one
   `{"t": <tool>, "canvas_id": <unit>, "tray_id": <tray>}` per G-code tool that
   got a tray. Every entry has all three fields: a partial entry is acknowledged
   and then printed from tray 0. Tools without a tray are left out, which leaves
   the choice to the printer. An empty `slot_map` lets the printer choose
   everything.

## CANVAS

Both protocols report the same `canvas_info`: `canvas_list[]` of units
(`canvas_id`, `connected`) with `tray_list[]` (`tray_id`, `filament_type`,
`filament_color` `#RRGGBB`, `filament_name`, `brand`, nozzle range, `status`
0 empty / 1 loaded / 2 feeding). Centauri Carbon 1 reports `?` and dashes for
unknown fields; those are treated as unknown. The units appear in the printer
card as CANVAS 1, 2 with the "Untested on hardware" note.

Not verified anywhere public: whether a map naming only some tools makes the
printer honour the others, and how `canvas_id` behaves with more than one
CANVAS. The engine sends complete entries for every tool it remaps.

## Status mapping

- SDCP `CurrentStatus` 1 (printing) with `PrintInfo.Status` 3 printing, 5/6
  pausing/paused, 7/8 stopping/stopped, 9 complete, 114 error, and preheating,
  levelling and homing sub-states shown as preparing. `CurrentTicks` and
  `TotalTicks` are milliseconds per the SDCP document; some FDM firmware reports
  seconds, so values above 400000 are read as milliseconds.
- CC2: `machine_status.status` 2 with `sub_status` 2501/2502/2505 paused,
  2077 completed, 2503/2504 stopped, heating and homing as preparing; otherwise
  `print_status.state` (Klipper's `printing`, `paused`, `complete`,
  `cancelled`, `error`, `standby`). Method 6000 sends deltas after the first
  full status; the engine merges them and asks for a full status every five
  minutes.

## What to check on a real printer

- The card connects, shows temperatures and follows a print from start to end
  (the completion lands in the print log with the right outcome).
- Pause, resume, stop and the light work; temperature targets apply.
- A queued `.gcode` uploads and starts; with a CANVAS, the trays print the
  colours the queue item's `ams_mapping` names.

## Sources

- SDCP v3.0.0 specification, github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0
  (topics, request framing, Cmd 0/1/128-131, status fields, the upload endpoint).
- Elegoo's elegoo-link SDK (Apache-2.0), github.com/ELEGOO-3D/elegoo-link:
  `src/lan/adapters/elegoo_fdm_cc/` (Cmd 128 with `slot_map`, Cmd 324 CANVAS,
  status parsing, chunked upload) and `elegoo_fdm_cc2/`.
- elegoo-homeassistant (MIT), github.com/danielcherubini/elegoo-homeassistant:
  `docs/CC2_PROTOCOL.md` (CC2 MQTT registration, heartbeat, methods, delta
  status, CANVAS, the measured `slot_map` and upload behaviour, packet captures
  of ElegooSlicer) and its SDCP status and CANVAS tests.

No code was copied; the projects are cited for protocol facts and sample
payloads.
