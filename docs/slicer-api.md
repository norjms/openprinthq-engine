# Slicer-facing API

What a slicer plugin needs from a print farm, and the OpenPrintHQ engine call
that answers each need. Written against the Watchtower print-queue plugin for
OrcaSlicer, which today talks to Watchtower's public API v1 (API key) and its
undocumented internal web API (a signed-in browser session). Every need below is
served by the engine's versioned, documented API (`docs/openapi.json`) with one
API key; no session, cookie or CSRF dance.

## Reaching the engine

| | |
|---|---|
| Base | `<OpenPrintHQ URL>/api/engine/api/v1` (through the control plane) |
| Auth | `Authorization: Bearer ophqk_...` (an OpenPrintHQ API key, scope `control` for anything that changes state) |
| Identity check | `GET <OpenPrintHQ URL>/api/me` answers `apiKey` for a key |
| Scopes | Through the control plane: every `GET` needs `read`; `POST /queue/` and `POST /library/files` need `submit`; every other write needs `control`, including the read-only `POST /queue/preflight` dry run. A slicer plugin that runs the pre-send check, edits the queue or sets nozzles needs a `control` key |
| Engine keys | An engine's own `bb_` key works against an engine reached directly (`<engine>/api/v1`), with that key's permission flags |
| Errors | JSON `{"detail": ...}`; `detail` is a string, or `{"code", "message"}` for typed errors such as `unsupported_for_printer_type` |

A key acts with its owner's printer scope (`docs/permission-scoping.md`): if
the owner's group is limited to some printers, the printer list, preflight,
queue and camera calls only ever show and accept those printers, and any other
printer answers 404 as if it did not exist. A key made by an administrator or
a member of an unlimited group sees the whole farm.

## Map

| Plugin need | Watchtower call today | OpenPrintHQ engine |
|---|---|---|
| Connect, check the key | `GET /api/public/v1/ping` | `GET /api/me`, then `GET /printers/` |
| Printer list and live state | `GET /printers`, `GET /printers/{name}` | `GET /printers/` (includes `controls`), `GET /printers/{id}/status` |
| What each printer can do | (none) | `controls` on each printer: `pause_resume_stop`, `print`, `home`, `temperature`, `gcode` |
| Upload a sliced file | `POST /files` (multipart) | `POST /library/files` (multipart, field `file`) |
| Skip an upload the farm already has | `GET /api/library/files` (internal), compare hashes | `GET /library/files/by-hash/{sha256}`: the stored file, or 404; or upload with `?reuse_duplicate=true` and get the stored file back (`reused: true`) |
| Queue a job (copies, priority, one printer or any of a model) | `POST /jobs` | `POST /queue/` with `library_file_id`, `quantity`, `priority` (0-100), and `printer_id` or `target_model` / `target_location` |
| Job status | `GET /jobs/{id}` | `GET /queue/{id}` |
| Pause, resume, cancel a job | `POST /jobs/{id}/{action}` | `POST /printers/{id}/print/pause`, `.../resume`; `POST /queue/{id}/cancel` with an optional `{"reason", "by"}` (not started) or `POST /queue/{id}/stop` (printing) |
| Printer commands (set ready, home, temperatures, light) | `POST /printers/{name}/commands` | `POST /printers/{id}/clear-plate`, `/home-axes`, `/temperature/nozzle`, `/temperature/bed`, `/chamber-light`; check `controls` first |
| The farm queue | `GET /api/queue` (internal) | `GET /queue/` |
| Reorder | `POST /api/queue/reorder` (internal) | `POST /queue/reorder` with `{"items": [{"id", "position"}]}` |
| Priority | `POST /api/queue/job/{id}/priority` (internal) | `PATCH /queue/{id}` with `priority` |
| Print now on a printer | `POST /api/queue/job/{id}/print-now` (internal) | `PATCH /queue/{id}` with `printer_id` (and `target_model: null`) and `priority: 100`, then `POST /queue/reorder` to put it first. `POST /queue/{id}/start` only releases an item staged with `manual_start` |
| Remove from the queue | cancel, then `DELETE /api/queue/job/{id}` (internal) | `DELETE /queue/{id}` |
| Queue statistics | `GET /api/queue/stats` (internal) | `GET /queue/stats` |
| Pre-send check: nozzle size, filament loaded, printer down, wrong model | `POST /api/queue/preview-filament-availability` plus `/api/maintenance/down` and `/api/printers/nozzles` (internal) | `POST /queue/preflight` (below): one call, every printer, with reasons |
| Why a queued job is not printing | (none) | `GET /queue/{id}/preflight` |
| What each printer has loaded, slot by slot (filament sync button) | `GET /api/printers/{name}/ams_status`, `GET /api/filament/assignments` (internal) | `GET /printers/{id}/status` (`ams`, `vt_tray`); external printers: the spool assignments in `GET /inventory/assignments` |
| Printers out of service | `GET /api/maintenance/down` (internal) | `GET /printers/out-of-service` |
| Finished, failed, cancelled events | polling `GET /jobs/{id}` | polling `GET /queue/{id}`, or a webhook notification provider |
| Alarms | (none) | `GET /alarms`, `GET /alarms/summary` (`docs/alarm-feed.md`) |

Two Watchtower needs map differently:

- **Setting a printer's nozzle size from the slicer** (Watchtower
  `POST /api/printer/update`, which rewrites the whole printer record):
  `PUT /printers/{id}/nozzles/{extruder_index}` with
  `{"diameter_mm", "nozzle_type", "volume_type"}` sets one extruder;
  `GET /printers/{id}/nozzles` reads them all. The override is what the
  pre-send check and the dispatcher compare against for that extruder, also on
  printers that report no nozzles (PrusaLink, OctoPrint). Through the control
  plane this needs a `control` key; an engine's own `bb_` API key cannot change
  printer settings at all (`PRINTERS_UPDATE` is not an API-key permission).
- **Recolouring a manually tracked spool so a job is not held** (Watchtower
  `PUT /api/filament/assignment`): not needed. Watchtower holds a job until the
  loaded colour matches; the engine's dispatcher holds only on material type
  (`missing filament: <type>`), and uses colour just to prefer the printer whose
  slots match best. A slicer that still wants to correct a spool's colour edits
  the spool in the inventory (`PATCH /inventory/spools/{id}`).

## POST /queue/preflight

The pre-send check, before anything is queued. Same checks, and the same
per-printer answer, as `GET /queue/{id}/preflight` for a queued job: the two
share `_check_printers` in `api/routes/print_queue.py`.

Body (every field optional):

| Field | Meaning |
|---|---|
| `library_file_id` | An uploaded file. Its metadata fills nozzle size, plate and sliced-for model; its 3MF filament list fills the materials |
| `printer_id` | Check only this printer |
| `target_model`, `target_location` | Check only printers of this model (and location) |
| `filament_types` | Materials the job uses, such as `["PLA", "PETG-CF"]`. Upper-cased |
| `nozzle_diameter`, `bed_type`, `sliced_for_model` | Explicit needs; they win over the file's metadata |
| `skip_filament_check` | Leave the loaded-filament check out |

With no printer or model, every active printer is checked. For a key whose
owner is limited to some printers, only those printers are checked, and a
`printer_id` outside them answers 404.

Answer:

```json
{
  "library_file_id": 41,
  "job_plate": "Textured PEI Plate",
  "filament_types": ["PETG"],
  "target": {},
  "needs": {"nozzle_diameter": 0.4, "bed_type": "Textured PEI Plate", "abrasive": [], "filament_types": ["PETG"], "sliced_for_model": "X1C"},
  "eligible": ["H2C"],
  "printers": [
    {"printer_id": 1, "name": "H2C", "model": "H2C", "eligible": true, "reasons": [], "has_plate": "Textured PEI Plate", "plate_mismatch": false},
    {"printer_id": 4, "name": "MK4S", "model": "MK4S", "eligible": false, "reasons": ["missing filament: PETG", "file sliced for X1C"], "has_plate": null, "plate_mismatch": false}
  ]
}
```

Reasons are short phrases: `offline`, `busy`, `plate not cleared`,
`out of service: <why>`, `missing filament: <types>`,
`needs a 0.6mm nozzle (has 0.4mm)`, `needs a hardened nozzle for PLA-CF ...`,
`file sliced for <model>`. `plate_mismatch` marks a printer whose only problem
may be the fitted plate.

## GET /library/files/by-hash/{sha256}

`sha256` is the file's SHA-256 in hex (64 characters, any case). Answers the
stored file (`id`, `filename`, `file_type`, `file_size`, `file_hash`,
`folder_id`, `created_at`, `metadata`) or 404. Files in the trash do not count.
A user who may only read their own library files only finds their own. 400 for
anything that is not 64 hex characters.

A plugin hashes the G-code it is about to send, asks here, and on a hit queues
`POST /queue/` with the existing `library_file_id` instead of uploading again.
