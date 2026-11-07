# Non-Bambu printers: what each connection type can do

OpenPrintHQ talks to printers over several transports. `Printer.connection_type`
picks the client, and `services/printer_capabilities.py` is the single source of
truth for what each one supports. The printer API exposes the result as
`controls` so clients can hide what a printer cannot do.

| Connection type | Monitor | Pause, resume, stop | Print | Home | Temperatures | G-code |
|---|---|---|---|---|---|---|
| `bambu` | yes | yes | yes | yes | yes | yes |
| `klipper` (Moonraker) | yes | yes | yes | yes | yes | yes |
| `octoprint` | yes | yes | yes | yes | nozzle 0 and bed | yes |
| `prusalink` | yes | yes | yes | no | no | no |
| `duet`, `flashforge`, `mks`, `snapmaker` | yes | yes | yes | yes | nozzle 0 and bed | yes |
| `obico` | no | no | yes (upload only) | no | no | no |

## `controls`

`GET /api/v1/printers` and `GET /api/v1/printers/{id}` include:

```json
"controls": {"pause_resume_stop": true, "print": true, "home": false, "temperature": false, "gcode": false}
```

A missing key means supported (older engines send no `controls`). A route a
printer cannot serve answers `400` with
`{"code": "unsupported_for_printer_type", "message": "..."}`, never a 500 or a
silent success.

## PrusaLink

Buddy firmware (MK4, MK4S, MK3.9, MINI, XL) speaks PrusaLink. It answers `404`
to OctoPrint's `/api/printer/printhead`, `/api/printer/tool`, `/api/printer/bed`
and `/api/printer/command`, and its v1 API has no equivalent, so homing,
temperatures and raw G-code cannot be sent over the network. Verified against
an MK4S on firmware 6.5.7 (PrusaLink 2.1.2). `PrusaLinkClient.UNSUPPORTED_METHODS`
lists them; the routes check it through `_ensure_client_supports`.

Auth is the printer's API key in `X-Api-Key` (Settings, Network, PrusaLink on
the printer). The printer also advertises HTTP Digest; the API key works on
firmware 6.5.

How a print ended comes from the printer's state, not from progress:

| PrusaLink state | Engine state | Queue outcome when a print ends |
|---|---|---|
| `PRINTING` | RUNNING | |
| `PAUSED`, `ATTENTION` | PAUSE | none (the job resumes from the printer) |
| `FINISHED` | IDLE | completed |
| `STOPPED` | IDLE | cancelled |
| `ERROR` | FAILED | failed |
| `IDLE`, `READY` | IDLE | completed at 99 percent or more, else cancelled |

`ATTENTION` covers filament runout, MMU problems and crash detection: the
printer waits for someone and the job continues from the printer, so it must
not fail the queue item.

## OctoPrint

OctoPrint has no explicit success flag. A print that returns to Operational
counts as completed when its last progress was 99 percent or more, otherwise
cancelled. Temperatures go to `tool0` and the bed only.
