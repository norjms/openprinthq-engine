# Anycubic Kobra printers (LAN mode) and the ACE Pro

> **Untested on hardware.** OpenPrintHQ owns no Anycubic printer. The client is
> built from the LAN-mode protocol as documented and validated on a Kobra S1 Max
> by other open-source projects (sources below); their captured reports are the
> test fixtures in `backend/tests/_fixtures/anycubic/`. The add dialog and the
> printer card mark the type "Untested on hardware".

Connection type `anycubic`: Kobra 3, Kobra 3 Max, Kobra 3 V2, Kobra S1,
Kobra S1 Max (the generation with the signed LAN handshake), on stock firmware
with **LAN mode** switched on at the printer. Cloud mode is not supported.

## What works

| | |
|---|---|
| Monitor | state, progress, layers, time left, nozzle, bed and chamber temperatures, fan |
| Pause, resume, stop | MQTT `print` `pause` / `resume` / `stop` |
| Temperatures | MQTT `print` `update` (`target_nozzle_temp`, `target_hotbed_temp`) |
| Light | MQTT `light` `control` (chamber light, type 2) |
| Print a library `.gcode` | upload through the printer's OctoPrint-compatible API, then MQTT `print` `start` |
| ACE Pro slots | `multiColorBox` reports, polled with `getInfo` every 30 s |
| ACE slot remap (#52) | `ams_settings.ams_box_mapping` in the start command |
| Home, raw G-code | no (the LAN protocol has no raw G-code; the home route sends G28) |

## Connecting

1. The engine runs the LAN handshake: `GET http://<ip>:18910/info` gives a
   token, the control URL and the model id; a signed `POST` to the control URL
   (`sign = md5(md5(token[:16]) + ts + nonce)`) returns an AES-CBC encrypted
   blob (key `token[16:32]`, IV the reply's token) with the local broker
   (`mqtts://<ip>:9883`), the MQTT user and password, the device id and a client
   certificate and key. Nothing is stored: the credentials stay in memory and
   the certificate in a private temporary directory removed on disconnect, and
   the handshake runs again on every reconnect.
2. MQTT over TLS with that certificate (the printer's own certificate is
   self-signed and not verified). The engine subscribes to
   `anycubic/anycubicCloud/v1/printer/public/<model>/<device>/#` and sends
   queries and controls to `.../web/printer/<model>/<device>/<type>`; the print
   start goes to `.../slicer/printer/...` as Anycubic's slicer sends it.
3. `info` is polled every 30 s (the printer pushes temperatures within a
   second through `tempature` reports, but the job's lifecycle only arrives in
   `info`, which it otherwise sends rarely). Progress comes from `print`
   reports that carry `progress`.

The port field is the upload port: 80 on stock firmware (the "Anycubic Orca
compatibility API", OctoPrint-shaped). Rinkhals moves it to 71.

## Status mapping

`info.project.state`: `preheating`, `auto_leveling`, `vibrating`,
`flow_calibrating` -> preparing; `printing`, `resuming`, `resumed` -> printing;
`pausing`, `paused` (or `project.pause` 1/2) -> paused; `stopping`, `stoped`
(sic) -> cancelled; `finished` -> completed. `remain_time` is in minutes,
`print_time` in seconds, `supplies_usage` in millimetres of filament.

## ACE Pro

`multiColorBox` reports carry `multi_color_box[]` (one entry per ACE, `id` from
0) with `slots[]`: `index`, `type` (material, empty for an empty slot),
`color` `[r, g, b]`, `status` (5 loaded, 4 ready), `consumables_percent`,
`sku`. Reports may carry one slot only, so the engine merges them by box and
slot. Box humidity and temperature only appear while the ACE is feeding; a
slim report carries humidity in `drying_status`. The slots show on the printer
card as ACE 1 with the "Untested on hardware" note.

The remap is sent as

```json
"ams_settings": {"use_ams": true, "ams_box_mapping": [
  {"paint_index": 0, "ams_index": 1, "paint_color": [67, 82, 59, 255], "ams_color": [67, 82, 59, 255], "material_type": "PETG"}
]}
```

one entry per G-code filament that got a slot (`ams_index` is the global slot,
`4 * box + slot`). Without an ACE, or when no filament matched, the start
command carries no `ams_settings` and the printer uses its own mapping.

## What to check on a real printer

- The handshake succeeds with LAN mode on and fails with a clear log line in
  cloud mode.
- The card follows a print through `info` and `print` reports and records the
  outcome; pause, resume, stop, the light and temperatures work.
- A queued `.gcode` uploads through port 80 and starts; with an ACE, the
  slots named by the queue item's `ams_mapping` feed the job.
- Not verified anywhere public: the start command with `ams_settings` was
  documented from Anycubic's slicer traffic (Rinkhals) but no capture of the
  printer honouring a remapped `ams_box_mapping` sent by a third party exists.

## Sources

- kobra-connect (Apache-2.0), github.com/rvanderp3/kobra-connect: the LAN
  handshake (`handshake.py`) and `docs/mqtt-commands.md` (topics, start print
  with `ams_settings`).
- anycubic_ha_local (AGPL-3.0), github.com/chrisfore/anycubic_ha_local:
  `research/PROTOCOL-VALIDATED.md` (captured on a Kobra S1 Max: report types,
  lifecycle, ACE fields, control commands, port map including the
  OctoPrint-compatible API on port 80) and the test fixtures reused here.
- Rinkhals (MIT for its scripts), github.com/jbatonnet/Rinkhals:
  `docs/firmware/mqtt.md` (print start with `ams_box_mapping`) and
  `docs/Rinkhals/network-activity.md` (port 80 / 71).
- hass-anycubic_cloud (GPL-3.0): the `multiColorBox` report handling and the
  `ams_box_mapping` entry shape.

No code was copied; the projects are cited for protocol facts and sample
payloads.
