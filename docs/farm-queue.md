# Farm queue: hardware matching, retries, preflight, stats, bed ejection, failure detection, priority, out of service and the maintenance log

These features make the queue behave like a print farm's dispatcher. They apply to queue items aimed at a
printer model (`target_model`) or a printer group (`target_group_id`); an item pinned to one printer keeps
the existing start-time checks.

## Hardware matching

When the scheduler looks for an idle printer for an item, each candidate must fit the file:

| Check | The file says | The printer says | Blocks when |
|---|---|---|---|
| Nozzle size | sliced nozzle diameter (archive or library metadata) | reported nozzle diameters | no installed nozzle is within 0.05 mm |
| Hardened nozzle | a filament type with CF or GF (PLA-CF, PA6-GF, PETG CF, ...) | reported nozzle material | no nozzle is `hardened_steel` or tungsten carbide |
| Build plate | the plate type it was sliced for | `installed_plate` on the printer | both are known and differ |

Plate names are compared loosely ("Textured PEI Plate", `textured_pei_plate` and "textured pei" are the
same). Set the plate with `PATCH /api/v1/printers/{id}` and `{"installed_plate": "Textured PEI Plate"}`;
`null` turns the check off for that printer.

Missing information never blocks: a file without a sliced nozzle or plate, a printer that has not reported
its nozzles yet, or a printer with no plate set are all treated as compatible.

A printer that doesn't fit is skipped and named in the item's waiting reason, for example
`Hardware: P1S-2 (needs the Textured PEI Plate (has Cool Plate))`. Unlike "all printers are busy", this
reason sends the waiting notification.

## Automatic retry

Set `max_retries` (0 to 10) when creating or editing a queue item. When the print fails and
`retry_count < max_retries`, a copy is queued at the front of the queue:

- the failed item keeps its status and error, so history and integrations see the failure;
- the copy has `retry_count` one higher and `retry_of_id` pointing at the first attempt;
- a model or group job may land on another printer; a pinned job stays on its printer;
- the next start waits for the plate to be cleared, as any start does.

## Preflight

`GET /api/v1/queue/{id}/preflight` answers which printers could take the item now:

```json
{
  "item_id": 42,
  "status": "pending",
  "target": {"model": "X1C", "location": null},
  "needs": {"nozzle_diameter": 0.4, "bed_type": "Textured PEI Plate", "abrasive": [],
            "filament_types": ["PLA"], "sliced_for_model": "X1C"},
  "waiting_reason": "Hardware: X1C-1 (needs the Textured PEI Plate (has Cool Plate)) | Busy: X1C-2",
  "printers": [
    {"printer_id": 1, "name": "X1C-1", "model": "X1C", "eligible": false,
     "reasons": ["needs the Textured PEI Plate (has Cool Plate)"]},
    {"printer_id": 2, "name": "X1C-2", "model": "X1C", "eligible": false, "reasons": ["busy"]}
  ]
}
```

Reasons: `offline`, `busy`, `plate not cleared`, `missing filament: ...`, `needs ...` (hardware) and
`file sliced for ...`.

## Marking an outcome by hand

`POST /api/v1/queue/{id}/mark` with `{"outcome": "completed" | "failed", "note": "...", "retry": false}`
works on items in `printing` and records how the print ended. It only changes the queue record. With
`"outcome": "failed"` and `"retry": true` a copy is queued as above, even when the item had no retries set.

## Statistics

`GET /api/v1/queue/stats?days=30` counts items created in the window:

```json
{"days": 30, "total": 120, "by_status": {"completed": 96, "failed": 8, "pending": 16},
 "success_rate": 0.923, "avg_wait_seconds": 1840, "avg_print_seconds": 7310, "retried_items": 5,
 "printers": [{"printer_id": 1, "completed": 50, "failed": 3, "cancelled": 1, "success_rate": 0.943}]}
```

## Bed ejection (continuous printing)

A printer can push a finished part off the bed itself and start the next job without anyone at it. Set an
**ejection profile** on the printer (Printers, edit, Ejection profile) and tick **Eject automatically after a
successful print**. Profiles live under `GET/POST /api/v1/ejection-profiles` and `PATCH/DELETE
/api/v1/ejection-profiles/{id}`.

After a print the plate-clear gate goes up as always. Then, only when the print **completed** (never after a
failure or a cancel, which may have left a mess on the bed):

1. The installed plate must be one the profile allows (`allowed_plates`, compared loosely like hardware
   matching; empty means any plate).
2. Fewer than `max_consecutive` automatic ejects in a row (0 = no limit). Clearing the plate by hand resets the
   count, so a person looks at the bed every N parts.
3. The engine waits until the bed is at or below `max_bed_temp_c` (parts release from a cold plate), up to
   `timeout_s`.
4. It sends the profile's `gcode` (a Klipper macro name or G-code lines) through the printer client.
5. After `settle_s` it waits for the printer to report idle again, up to `timeout_s`.
6. With `verify_with_camera`, the camera plate detector must see an empty bed (needs plate detection
   calibrated).
7. Only then is the gate cleared, so the queue can start the next job.

Any refusal, timeout or error leaves the gate up: the printer waits for a person exactly as without
auto-eject, and `GET /api/v1/printers/{id}/eject` says why (`last.reason`). While an eject runs the printer
is busy to the scheduler whatever `require_plate_clear` says, so a job can never start under a moving
toolhead. `POST /api/v1/printers/{id}/eject` runs the profile by hand on an idle printer (needs
`printers:control`; the plate and count limits don't apply because a person decided).

Test a profile by hand before turning it on: a wrong sequence can drive the toolhead into the part. Bambu
printers take the G-code over MQTT (`gcode_line`), which suits short sequences.

## Failure detection

Every detector that can tell a print went wrong records an event in one place, `GET /api/v1/failure-events`
(filter with `printer_id` and `unacknowledged=true`):

| source | where it comes from |
|---|---|
| `bambu_hms` | the printer's own AI monitor: spaghetti (`0300_8003`, `0C00_8042`), nozzle clumping (`0C00_803F`, `0C00_8043`) |
| `obico` | the Obico detector, with its score and the action it took |
| `klipper` | a Klipper print that ended in error, with Klipper's message |
| `stall` | progress and layer unchanged for `stall_minutes` while printing (off by default) |

Policy, `GET/PUT /api/v1/failure-detection/policy` with `{"hold_printer": bool, "stall_minutes": int}`:
with `hold_printer` on, a printer with an unacknowledged event is held, so the queue starts nothing on it until
someone acknowledges (`POST /api/v1/failure-events/{id}/ack`, or every event on a printer with
`POST /api/v1/printers/{id}/failure-events/ack`). The printer card shows **Held: failure detected**; clicking it
acknowledges. Holds survive a restart. Detection never pauses or stops a print by itself; the printer's own AI
halt and the Obico action setting keep their own behaviour.

## Priority

Every queue item has a `priority` from 0 to 100, on Watchtower's scale: 25 low, 50 normal (the default), 75 high,
100 urgent. Within one queue scope (a printer, or the unassigned jobs for a model or group) the dispatcher takes
higher priority first and keeps queue-position order between equal priorities, so a queue where everything is 50
behaves exactly as before. With shortest-job-first on, priority comes first and the SJF order applies within a
priority. Set it in the print dialog's schedule options, at `POST /api/v1/queue/` or with
`PATCH /api/v1/queue/{id}` `{"priority": 75}`. Automatic retries keep the priority of the failed attempt.

The queue list (`GET /api/v1/queue/`) is still ordered by position, as the drag-to-reorder UI expects; the
dispatch order is priority, then position.

## Out of service

A printer can be taken **out of service**: it stays connected and monitored (status, camera, temperatures,
history), but the queue gives it no new jobs. That is the difference from Maintenance Mode (`is_active` false),
which disconnects the printer altogether.

- Jobs pinned to the printer wait (skip reason `printer_out_of_service`); preflight lists
  `out of service: <reason>` for it.
- Model and group jobs go to other printers; when none is left the waiting reason reads
  `Out of service: <names>`, which is not "busy only", so the waiting notification fires.
- Auto-drying skips it. A print already running is not touched.

API: `PUT /api/v1/printers/{id}/out-of-service` with `{"reason": "..."}` (up to 200 characters),
`DELETE /api/v1/printers/{id}/out-of-service` to put it back, `GET /api/v1/printers/out-of-service` for the list
(`printer_id`, `name`, `model`, `reason`, `since`). `PATCH /api/v1/printers/{id}` also takes `out_of_service`
and `out_of_service_reason`. `out_of_service_since` is set when the printer goes out and kept while it stays out,
so changing the reason does not reset it. In the UI: the printer's edit dialog, and a pill on the printer card.

## Maintenance log

`maintenance_history` holds both kinds of maintenance record, so each printer has one history:

- **scheduled**: written when a maintenance item is marked done (`POST /api/v1/maintenance/items/{id}/perform`,
  which now also takes `performed_by`);
- **free-form**: work with no schedule, `POST /api/v1/maintenance/printers/{id}/log` with
  `{"title", "category", "notes", "performed_by", "performed_at"}`. Categories: inspection, cleaning,
  lubrication, calibration, consumable, repair, upgrade, other. Logging does not reset any schedule.

`GET /api/v1/maintenance/printers/{id}/log?limit=100` returns both, newest first, each with `scheduled`,
`title` (the maintenance type's name for scheduled rows), `category` (`scheduled` for scheduled rows),
`performed_by`, `performed_at` and the print hours at the time. `DELETE /api/v1/maintenance/log/{id}` deletes a
free-form entry; scheduled records answer 409. `performed_by` defaults to the signed-in user. The Maintenance
page shows the log under each printer when it is expanded.

Schema: `printer_maintenance_id` became nullable and the table gained `printer_id`, `title`, `category` and
`performed_by`. PostgreSQL drops NOT NULL in place. SQLite cannot, so the startup migration rebuilds the table
once, only while the old `NOT NULL` definition is still there, and checks the row count before dropping the old
table. Existing rows get `printer_id` from their schedule.

## Hooks for plate-aware clients

The engine holds a job whose sliced plate differs from a printer's `installed_plate`, and says why. A client
that can reslice (Artisan Hub) uses two hooks to turn that hold into a print, and keeps every decision on its
side:

- `GET /api/v1/queue/{id}/preflight` returns `job_plate` (the plate the file was sliced for) and
  `filament_types`, and each printer row carries `has_plate` and `plate_mismatch`. A printer whose only reason
  is the plate is a candidate for a reslice; the text reasons are unchanged.
- `PATCH /api/v1/queue/{id}` takes `library_file_id` on a pending item, usually with `printer_id` (and
  `target_model: null`) to pin it to the printer the file was resliced for. The item keeps its id, position,
  priority and retries. The new file must be sliced for that printer's model; its AMS mapping, plate id and
  any archive link are cleared because they belonged to the old file.

Plate names are compared after normalising, and Bambu Studio's "Cool Plate (SuperTack)" and OrcaSlicer's
"Supertack Plate" count as the same plate.

## Permissions and API keys

Preflight and stats are reads (`queue:read_*`); mark needs `queue:update_*`. Through OpenPrintHQ's scoped
integration keys, preflight and stats work with a `read` key and mark needs a `control` key.

Out of service needs `printers:update` (the list is `printers:read`). The maintenance log reads with
`maintenance:read`, writes with `maintenance:update` and deletes with `maintenance:delete`. Through integration
keys: listing works with a `read` key; taking a printer out of service, logging and setting priority need a
`control` key.
