# Filament matching

Before a queued job starts, the scheduler checks that the filament loaded on the printer fits the filament the
file was sliced with. A job that doesn't fit waits, says why, and is listed as unmatched so the hub can ask
someone to map it. Code: `services/filament_match.py` (pure functions), the scheduler (`_hold_for_filament`,
`_select_idle_printer`), routes `api/routes/filament_matching.py`.

## How a slot matches

Each colour slot of the file is matched to one loaded tray (AMS slot or external spool), each tray used once,
on the slot's own nozzle (any nozzle when a filament track switch is installed).

- **Material:** the tray's material group must be the slot's. Groups: `PLA`, `PLA Basic`, `PLA+`, `PLA Pro`,
  `PLA HF` are PLA; `PETG`, `PETG HF`, `PETG Basic` are PETG; `PA-CF`, `PA12-CF`, `PAHT-CF` are PA-CF; and so
  on (`DEFAULT_GROUPS`). A CF or GF fill is part of the group (`PLA-CF` is not `PLA`). A finish (Matte, Silk,
  Galaxy, ...) must agree only when both sides name one. More names: the `filament_material_groups` setting.
- **Colour, at the job's level:**

| Level | Fits when |
|---|---|
| `exact` | the same filament (same Bambu filament id and delta E 6 or less), or delta E 2 or less |
| `close` (default) | delta E 6 or less |
| `family` | the same colour family: black, white, grey, red, orange, yellow, green, blue, purple, pink, brown |
| `any` | material only |

Delta E is CIEDE2000. A colour that can't be read on either side never blocks.

- **Saved mappings** (`filament_aliases`): a requested filament and a loaded one someone accepted with
  "remember". They count as an exact match on every printer.

## When it applies

Only to printers that report what is loaded (Bambu Lab today). It is skipped for an item with
`filament_overrides` (its own rules), `skip_filament_check`, `use_ams` off, a mapping someone confirmed
(`filament_confirmed`, set by the mapping route, by an `ams_mapping` given when queueing or patched later), or
a file whose filaments can't be read.

- **Job pinned to a printer:** when the printer is free, the mapping the job would be sent with is checked. If
  every slot fits it is kept (prefer-lowest and the other mapping rules still choose among good trays); if not,
  the best fitting mapping replaces it; if none fits, the job waits and holds that printer's queue, so later
  jobs don't jump it.
- **Job for a model or group:** a free printer whose filament doesn't fit is skipped. When every free printer
  was skipped only for filament, the job is unmatched.

An unmatched job gets `filament_unmatched` (JSON: `since`, `level`, and per printer its `summary` and `slots`)
and a waiting reason such as `Filament doesn't match: H2D: filament 2 PETG #FFFFFF (closest: PLA #FFFFFF)`.
The engine's "job waiting" notification goes out once. Changing a job's printer or file clears both the
unmatched record and a confirmed mapping.

## API

| Method | Path | What |
|---|---|---|
| GET | `/api/v1/filament-matching/settings` | `level`, `levels`, `material_groups` (yours), `default_material_groups`, `limits` |
| PUT | `/api/v1/filament-matching/settings` | `{"level": "close", "material_groups": {"PLA PRO MAX": "PLA"}}` (settings:update) |
| GET | `/api/v1/filament-matching/unmatched` | Unmatched pending jobs, first in queue order first |
| GET | `/api/v1/filament-matching/queue/{id}` | What the job needs, and per printer: loaded trays (with labels such as `AMS A slot 2`), the best mapping at the job's level, per slot `suggested`, `grade`, `delta_e`, `needs_reslice` (another material group), and `same_model`. `all_printers=true` offers every active printer |
| POST | `/api/v1/filament-matching/queue/{id}/mapping` | `{"printer_id", "slots": [{"slot_id", "global_tray_id"}], "remember": true, "library_file_id"?, "by"?}`: pins the job to the printer with that mapping, confirmed; `library_file_id` swaps in a resliced file |
| GET | `/api/v1/filament-matching/aliases` | Saved mappings, newest first |
| DELETE | `/api/v1/filament-matching/aliases/{id}` | Forget one |

Queue items also take and return `match_level` (`exact`, `close`, `family`, `any`, or null for the farm
default), and return `filament_confirmed` and `filament_unmatched`.
