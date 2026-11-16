# Alarm feed

One list of the problems every printer is reporting right now, whatever the
transport, with history. Code: `services/alarm_feed.py`, routes
`api/routes/alarms.py`, table `printer_alarms`.

The failure timeline (`/failure-events`, `docs/farm-queue.md`) answers a
different question: "did a print go wrong?" (spaghetti, a stall, a Klipper
print that ended in error), with the optional hold on the printer. The alarm
feed answers "what is each printer complaining about?" and has no hold.

## What raises an alarm

| Source | Alarm | Code | Severity |
|---|---|---|---|
| `bambu` | Any HMS code of severity fatal (1), serious (2) or common (3); info (4) is left out, and so is `0500_400E` (the printer's own cancel). The severity of an `hms[]` entry is its message level, the high half of `code` (as Bambu Studio reads it; `attr` bits 8-15 are the part id). Codes Bambu publishes with no text are internal and left out, as Bambu Studio does (`HMS_INTERNAL_CODES`). Text from the bundled HMS catalog, by the 16-character long code first, since different HMS codes share a short code: `HMS_LONG_DESCRIPTIONS`, then `data/hms_texts.json` (every code Bambu Studio publishes text for, in every printer family; rebuild it with `scripts/update_hms_texts.py`), then the short code. | HMS short code, for example `0700_8010` | fatal, serious, warning |
| `klipper` | Klippy shutdown or disconnect | `klippy_shutdown`, `klippy_disconnected` | fatal |
| `klipper` | A print that ended in error, with Klipper's message (a cancel has no message and raises nothing) | `print_error` | serious |
| `prusalink` | ATTENTION: filament runout, MMU or a detected crash; the printer waits for someone | `attention` | warning |
| `prusalink` | ERROR | `printer_error` | serious |
| `octoprint`, `duet`, `flashforge`, `mks`, `snapmaker` | The printer's error state | `printer_error` | serious |

A Bambu printer whose last print failed is not in an alarm state by itself; its
HMS codes are what count. Obico printers report no live state, so they raise
nothing here.

## Lifecycle

- `observe()` runs on every printer status change. A new alarm inserts a row
  (`first_seen`); one still reported refreshes `last_seen` at most once a
  minute.
- An alarm the printer stops reporting is cleared (`cleared_at`) after 30
  seconds, so a code that flaps on and off (Bambu chamber temperature
  warnings do) stays one row.
- Nothing is cleared while the printer is disconnected.
- Open rows are reloaded after a restart, so an alarm that was active before
  keeps its row.
- Acknowledging marks an alarm seen; it stays active until the printer stops
  reporting it.
- A code that comes back within 15 minutes of clearing reopens its alarm,
  which keeps who acknowledged or dismissed it. Some codes come and go for
  hours (an H2D reports a dirty nozzle camera lens for about 2.5 minutes,
  drops it for about one, and reports it again); after a longer gap it is a
  new alarm.
- Dismissing takes it out of the active list (and the summary) now, and marks
  it acknowledged. The row stays open while the printer reports the code, and
  stays dismissed through short gaps.
- Suppressing a code on a printer (`alarm_suppressions`) stops the feed
  recording it there and stops Bambu HMS notifications for it, and dismisses
  its open alarm. Removing the suppression brings it back, unseen, the next
  time the printer reports it.
- Bambu alarms keep the full HMS code (`full_code`). A row whose text changes
  (a catalog update) is rewritten while the printer still reports it.
- Cleared rows are deleted after 90 days.

A Klipper print that ended in error stays an alarm while Klipper's print state
is `error`, which lasts until the next print starts or Klipper is restarted.
Acknowledge it to mark it handled.

## Notifications

Bambu HMS errors already notify from the HMS handler in `main.py`, so the feed
does not notify for them. For the other transports it sends the provider's
"printer error" event (`on_printer_error`, type "Printer alarm") for new fatal
and serious alarms and for PrusaLink ATTENTION.

## Web page

**Alarms** in the sidebar (`/alarms`, `frontend/src/pages/AlarmsPage.tsx`) lists the
feed, worst first, and refreshes every 30 seconds. Include cleared alarms to see
history; pick a printer to filter and to acknowledge everything on it.
Acknowledging needs `printers:control`; seeing the page needs `printers:read`.

## API

| Method | Path | What |
|---|---|---|
| GET | `/api/v1/alarms` | `state=active` (default), `cleared` or `all`; `printer_id`; `severity` (repeat for several); `unacknowledged=true`; `limit`, `offset`. Active alarms come worst first, then newest. |
| GET | `/api/v1/alarms/summary` | `active`, `unacknowledged`, `by_severity`, and the `printers` with any active alarm, for a badge |
| GET | `/api/v1/alarms/{id}` | One alarm |
| POST | `/api/v1/alarms/{id}/ack` | Acknowledge one, body `{"by": "name"}` optional |
| POST | `/api/v1/printers/{id}/alarms/ack` | Acknowledge every unacknowledged alarm on a printer |
| POST | `/api/v1/alarms/{id}/dismiss` | Dismiss one, body `{"by": "name"}` optional |
| POST | `/api/v1/alarms/{id}/suppress` | Stop alerting about this alarm's code on its printer, and dismiss it; returns the suppression |
| GET | `/api/v1/alarm-suppressions` | The suppressed codes, newest first; `printer_id` to filter |
| DELETE | `/api/v1/alarm-suppressions/{id}` | Alert about that code again |

`GET /alarms` with `state=active` leaves dismissed alarms out unless
`include_dismissed=true`. Reading needs `printers:read`; acknowledging,
dismissing and suppressing need `printers:control`.
Each alarm: `id`, `printer_id`, `printer_name`, `source`, `code`, `severity`,
`message`, `first_seen`, `last_seen`, `cleared_at`, `active`,
`acknowledged_at`, `acknowledged_by`, `full_code`, `hms_code` (as Bambu writes
it, `HMS_0C00-0100-0002-0017`), `dismissed_at`, `dismissed_by`. Each
suppression: `id`, `printer_id`, `printer_name`, `source`, `code`, `message`,
`created_at`, `created_by`.
