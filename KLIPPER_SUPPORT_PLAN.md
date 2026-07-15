# Plan: Klipper / Voron 2.4 printer support

Status: **IMPLEMENTED** (initial vertical slice). See "Implementation status" at the bottom.

## Scope (agreed)

- **Full functionality**: monitor + control + print (upload `.gcode` and start jobs).
- **Generic by design, Voron 2.4 as the only tested profile**: the transport (Moonraker) and the data layer are built **capability-driven** so adding another Klipper printer later is a new *profile entry*, not new plumbing. We hardcode + test the **Voron 2.4 (350×350, heated chamber, QGL, single extruder)** profile only; other printers are structurally possible but unverified.
- **Auth**: Vorons run **open on the LAN** (Moonraker trusted-clients) — no API key required by default. An **optional** API-key field is still provided for users who lock Moonraker down.
- **Live + lightweight history**: **no 3MF archive pipeline** (no `PrintArchive` rows, no thumbnails/plates/cost/filament tracking). But every Klipper run **does** write a `PrintLogEntry` row (`archive_id = NULL`) so date / printer / status / duration / name show up in the existing **Print Log** view. This reuses the purpose-built nullable-FK design of `print_log_entries`.
- Bambu code paths stay **untouched**; all Klipper work is additive.

## Background: how the app is wired today (why this is non-trivial)

The app has **no printer-protocol abstraction**. Everything is hardwired to Bambu:

- `PrinterManager` ([backend/app/services/printer_manager.py](backend/app/services/printer_manager.py)) stores one concrete `BambuMQTTClient` per printer (`self._clients[id] = BambuMQTTClient(...)`, `connect_printer()` ~L366).
- `BambuMQTTClient` ([backend/app/services/bambu_mqtt.py](backend/app/services/bambu_mqtt.py), ~5k lines) talks **MQTT/TLS**, owns the `PrinterState` dataclass (L137), and fires callbacks: `on_state_change`, `on_print_start/complete`, `on_print_running_observed`, `on_ams_change`, `on_layer_change`, `on_bed_temp_update`, `on_drying_complete`.
- `Printer` model ([backend/app/models/printer.py](backend/app/models/printer.py)) is Bambu-shaped: `serial_number` (**unique, required**), `access_code` (required), `model` as a Bambu enum that drives feature-detection (`supports_chamber_temp()`, `is_bed_slinger()`, `supports_drying()` in printer_manager.py).
- Print dispatch: `printer_manager.start_print()` delegates to `client.start_print(...)`; called from [background_dispatch.py](backend/app/services/background_dispatch.py) (L670, L868) and [print_scheduler.py](backend/app/services/print_scheduler.py) (L2146). The file path is **3MF over FTPS**, then an MQTT print command.
- `printer_state_to_dict()` (printer_manager.py L785) builds the WebSocket payload. **It parses AMS only from `raw_data["ams"]`** — so for a Klipper printer with no AMS in `raw_data`, `ams_units` is naturally empty. Good: the status payload degrades gracefully.

### Two facts that de-risk the mapping

1. **State vocabulary** is a small fixed set: `IDLE, RUNNING, PAUSE, FINISH, FAILED, PREPARE, SLICING, unknown` (bambu_mqtt.py).
2. **Temperature dict keys** consumed by `get_derived_status_name()` and the frontend: `bed`, `bed_target`, `nozzle`, `nozzle_target`, `chamber`, `chamber_target` (+ `*_heating` flags).

A `KlipperClient` just has to fill those.

### Moonraker mapping (what Klipper gives us)

Talk to Moonraker at `http://<ip>:7125` (WebSocket `ws://<ip>:7125/websocket`, JSON-RPC):

| Bambuddy need | Moonraker source |
|---|---|
| Live status | `printer.objects.subscribe` → `notify_status_update` |
| `state` | `print_stats.state`: standby→`IDLE`, printing→`RUNNING`, paused→`PAUSE`, complete→`FINISH`, error/cancelled→`FAILED` |
| `current_print`/`gcode_file` | `print_stats.filename` |
| `progress` | `virtual_sdcard.progress` (0–1 ×100); fallback `display_status.progress` |
| `layer_num`/`total_layers` | `print_stats.info.current_layer`/`total_layer` (needs slicer `SET_PRINT_STATS_INFO`; else estimate from progress) |
| `remaining_time` | **derived** (Moonraker has none): from `print_stats.print_duration` + progress, or slicer estimate |
| temps | `extruder` → nozzle/nozzle_target, `heater_bed` → bed/bed_target, chamber sensor (`heater_generic chamber` / `temperature_sensor chamber`) → chamber/chamber_target |
| `cooling_fan_speed` | `fan.speed` |
| pause/resume/cancel | `printer.print.pause` / `.resume` / `.cancel` |
| start | `printer.print.start {filename}` |
| arbitrary control | `printer.gcode.script {script}` (M104/M140, G28, QUAD_GANTRY_LEVEL, light macro, M112) |
| upload file | HTTP `POST /server/files/upload` (multipart, root=`gcodes`) |
| stable identity | `/machine/system_info` or `/server/info` machine UUID |
| auth (if enabled) | API key header / oneshot token; or Moonraker trusted-clients |

No AMS, drying, HMS, k-profiles, cloud, virtual printer, MakerWorld — all inert for Klipper.

---

## Implementation phases

### Phase 0 — Define the client abstraction (no behavior change)
- Inventory every method called on a client via `printer_manager` and `get_client()` across the codebase (AMS commands, drying, k-profiles, `start_print`, `stop_print`, pause/resume, light, set-temp, `check_staleness`, `enable_logging`, `request_status_update`, …).
- Add `backend/app/services/printer_client.py` defining a `PrinterClient` `typing.Protocol` (or ABC) for the **common** surface: `connect()`, `disconnect(timeout)`, `state: PrinterState`, `check_staleness()`, `start_print(...)`, `stop_print()`, `pause()/resume()`, `request_status_update()`, logging hooks.
- `BambuMQTTClient` already satisfies it (document, no code change). Bambu-only methods (AMS/drying) stay off the Protocol; callers guard with capability checks.

### Phase 1 — Data model + capabilities
- Add `connection_type: str` to `Printer` (default `"bambu"`, NOT NULL). Hand-rolled migration in `run_migrations()` ([backend/app/core/database.py](backend/app/core/database.py)) — add column with default.
- Make `serial_number` / `access_code` **nullable**; for Klipper synthesize a stable serial from Moonraker's machine UUID (`klipper:<uuid>`). Keep the unique index but allow the synthetic value.
- Add Klipper connection fields (reuse where possible): `moonraker_port` (default 7125), `moonraker_api_key` (nullable, **encrypted** via `core/encryption.py`). Camera reuses existing `external_camera_*` columns.
- **Profile registry (generality)**: `backend/app/services/klipper/profiles.py` maps a profile key → capability + geometry block (bed size, has_chamber, extruder count, control-macro names). Seed it with one entry, `voron_2.4_350`. Adding a printer later = one dict entry, no new code paths. `Printer.model` stores the profile key for Klipper rows.
- Capabilities helper: `printer_capabilities(printer) -> {has_ams, has_drying, has_chamber, has_kprofiles, is_klipper, ...}`, resolved from `connection_type` + the profile registry. Update `supports_*`/`is_bed_slinger` to consult it. Bambu = today's behavior; `voron_2.4_350` = `{has_ams:False, has_drying:False, has_chamber:True, has_kprofiles:False}`.
- Pydantic schemas (`PrinterCreate`/`PrinterResponse`): add `connection_type` + Moonraker fields; make serial/access optional; validate per type.

### Phase 2 — `KlipperClient` (Moonraker WS client)
- New package `backend/app/services/klipper/` — `moonraker_client.py` (status + control), `moonraker_files.py` (upload/list/delete).
- Async WS JSON-RPC client: connect, optional auth, `printer.objects.subscribe` for `print_stats, virtual_sdcard, display_status, extruder, heater_bed, <chamber>, fan, gcode_move, toolhead, webhooks`.
- On `notify_status_update`: update the **core** `PrinterState` fields per the mapping table; fire the same callbacks (`on_state_change`, `on_print_start` on transition→printing, `on_print_complete` on complete/error/cancelled, `on_layer_change`, `on_bed_temp_update`). Leave AMS/drying/HMS empty.
- `check_staleness()` from WS liveness/heartbeat. Reconnect with backoff.
- Control methods map to Moonraker RPCs / `gcode.script` (Voron macros).

### Phase 3 — Wire into `PrinterManager`
- `connect_printer()` branches on `printer.connection_type`: instantiate `KlipperClient` (same callback wiring) vs `BambuMQTTClient`.
- Type `get_client()` as `PrinterClient`. Audit/guard every Bambu-only call site (inventory/spool assignment, AMS history, drying, k-profiles, MakerWorld, cloud, virtual printer, Prometheus metrics) to **skip Klipper printers** by capability.

### Phase 4 — File upload + print dispatch (the "print" half)
- `moonraker_files.upload_gcode()` → `POST /server/files/upload`. Source = a `.gcode` in Bambuddy's Library.
- Dispatch branch in `background_dispatch` / `print_scheduler`: for Klipper printers, upload the library `.gcode` to Moonraker, then `client.start_print(remote_name)`. 3MF/plate/`ams_mapping` args ignored. **No archive creation.**
- **Lightweight history hook**: branch early in `on_print_complete()` ([backend/app/main.py:3324](backend/app/main.py)) on `connection_type == "klipper"` — write a `PrintLogEntry` via `print_log.write_log_entry(...)` (`archive_id=None`; populate `print_name`, `printer_name/id`, `status`, `started_at`, `completed_at`, `duration_seconds`, `created_by_*`) and **return before** the 3MF/archive block (main.py:3934+). Filament/cost/energy fields stay `NULL`.
- PrintModal/queue: allow selecting a Klipper printer for `.gcode` files; hide plate picker + AMS mapping for Klipper.

### Phase 5 — Camera (reuse existing)
- Voron uses the existing external-camera support: set `external_camera_url` to the Moonraker webcam stream (`/webcam/?action=stream`) and `external_camera_snapshot_url` (`?action=snapshot`).
- Nice-to-have: auto-populate from Moonraker `/server/webcams/list` during add.

### Phase 6 — Frontend
- `frontend/src/api/client.ts` `Printer` interface: add `connection_type` + optional Moonraker fields; serial/access optional.
- Add-printer dialog (in [frontend/src/pages/PrintersPage.tsx](frontend/src/pages/PrintersPage.tsx)): printer-type selector → **Bambu** (serial/access/IP) vs **Voron 2.4 (Klipper)** (name/IP/port/optional API key/camera URL).
- Printer card: gate AMS panel, drying, k-profiles, airduct, store-to-SD, HMS UI on capabilities. Voron card shows nozzle/bed/chamber temps, progress, layer, camera, and controls: pause/resume/stop, light, set temps, home (G28), **QGL** button, emergency stop.
- i18n: add all new strings to **all 11 locales** (`frontend/src/i18n/locales/*.ts`) — `npm run check:i18n` parity gate fails otherwise.

### Phase 7 — Voron 2.4 capability profile
- Hardcode: bed 350×350, single extruder, chamber present, control macros (`QUAD_GANTRY_LEVEL`, `G28`, chamber-light via `SET_PIN`/macro, `M104`/`M140`/`M141`, `M112`).

### Phase 8 — Tests
- Backend unit ([backend/tests/unit](backend/tests/unit)): `KlipperClient` status mapping from recorded `notify_status_update` payloads; state-vocab mapping; capability gating; upload request shape (mock WS + HTTP).
- Frontend (Vitest): Voron card renders without AMS; add-printer form per-type validation; i18n parity.
- Manual checklist against the real Voron: connect, live temps/progress, pause/resume/cancel, set temp, home, QGL, upload+print a small `.gcode`, camera.

---

## Resolved decisions
- **Auth**: open-LAN default; optional API-key field (no key required to use the feature).
- **History**: lightweight `PrintLogEntry` rows (no archive). See Phase 4 history hook.
- **Generality**: capability + profile-registry design; `voron_2.4_350` is the only seeded/tested profile.

## Risks / open questions
1. **Call-site audit completeness** *(top risk)* — missing a Bambu-only `get_client()` caller that assumes AMS/drying/k-profiles would crash on a Voron. Phase 0 inventory + capability guards are the mitigation.
2. **State granularity** — Klipper has no "preparing/heating" sub-states like Bambu's stages; `get_derived_status_name()` heating heuristic partly covers it. Acceptable?
3. **Remaining-time accuracy** — derived, not authoritative (`print_duration` extrapolation / slicer ETA); expect drift.
4. **Layer counts** depend on the slicer emitting `SET_PRINT_STATS_INFO`; otherwise estimate from progress.
5. **Identity** — synthetic serial from Moonraker machine UUID; confirm stable across reboots/reflashes.
6. **Discovery** — SSDP is Bambu-only; Voron added manually by IP (mDNS optional later).

## Suggested build order
Phase 0 → 1 → 2 (monitor working end-to-end first, read-only) → 3 → 6 (see a Voron card live) → 4 (printing) → 5/7 polish → 8 throughout.

---

## Implementation status

Done and verified (backend boots, migration applies, lint/type/i18n green, unit tests pass):

**Backend**
- `models/printer.py` — `connection_type`, `moonraker_port`, `moonraker_api_key`; `access_code` nullable. Migration in `core/database.py`.
- `schemas/printer.py` — connection_type + Moonraker fields; per-type validation; API key accepted on input, never serialised (response exposes `has_moonraker_api_key` bool).
- `services/klipper/profiles.py` — profile registry (`voron_2.4_350`).
- `services/printer_capabilities.py` — `capabilities_for()` / `is_klipper()`.
- `services/printer_client.py` — `PrinterClient` protocol.
- `services/klipper/status_map.py` — pure Moonraker→PrinterState mapping.
- `services/klipper/moonraker_client.py` — websocket client (status + control), satisfies `PrinterClient`.
- `services/klipper/moonraker_files.py` — gcode upload/list/delete over Moonraker HTTP.
- `services/printer_manager.py` — `connect_printer` branches Bambu vs Klipper.
- `services/background_dispatch.py` — `_dispatch_klipper_print` (upload .gcode + start; no archive).
- `main.py::on_print_complete` — Klipper branch writes a lightweight `PrintLogEntry` (archive_id NULL) and returns.
- `api/routes/printers.py` — `create_printer` Klipper path (synthetic serial, no MQTT pre-flight); Bambu-only control endpoints guarded with `_ensure_client_supports` (clean 400); new `/klipper/level`, `/klipper/emergency-stop`, `/klipper/set-temp`.

**Frontend**
- `api/client.ts` — `Printer`/`PrinterCreate` types + Klipper control methods.
- `pages/PrintersPage.tsx` — add-printer dialog connection-type toggle (Bambu / Voron-Klipper) with Moonraker fields; card hides print-speed for Klipper (AMS/airduct already data/model-gated).
- i18n — 8 new keys added + translated across all 11 locales.

**Tests** — `backend/tests/unit/services/klipper/` (status mapping, capabilities, client transitions): 19 passing.

### Follow-ups — done
- **Klipper version on the card**: `MoonrakerClient` queries `printer.info` on connect and maps `software_version` → `state.firmware_version`. (Synthetic-serial → machine-UUID swap intentionally **not** done: identity is by `printer_id`, and mutating the unique serial post-create risks constraint conflicts for no functional gain.)
- **Camera auto-detect**: `create_printer` (Klipper) best-effort queries Moonraker `/server/webcams/list` and populates `external_camera_url`/snapshot/type when a webcam exists and none is set. Relative URLs absolutised against the host. Never fatal.
- **Voron control cluster**: card menu shows Home all / Quad gantry level / Emergency stop for Klipper printers (gated on `printers:control`); MQTT-debug item hidden for Klipper. Backed by `/klipper/level`, `/klipper/emergency-stop`, `/home-axes`.
- **Backend guards**: `kprofiles.py` (4 endpoints) and `inventory.py` AMS helper now reject/skip Klipper cleanly (`reject_klipper_feature` / hasattr early-return). `firmware.py` confirmed safe (reads state only). Klipper control endpoints now require `state.connected`.

### Not yet done / future work
- Replace synthetic serial with Moonraker machine UUID (deliberately deferred — see above).
- Per-axis set-temperature input in the card (the `/klipper/set-temp` endpoint exists; only one-click actions are wired into the UI).
- **Verify against real Voron 2.4 hardware** (live status, control, upload+print, camera) — the only thing that genuinely needs a printer.
