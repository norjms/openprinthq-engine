# Nozzles, filament paths and printer locations

OpenPrintHQ engine additions that let a client (for example a phone app) answer
three questions about every printer: which nozzles are fitted, which slot can
feed which nozzle, and which spool is installed where. Location rows gain a
hierarchy and printable scan codes so a spool can be moved or installed by
scanning a label.

All routes are under `/api/v1`. Every schema change is applied by
`run_migrations` on both SQLite and PostgreSQL.

## Vocabulary

The names follow OrcaSlicer / Bambu Studio.

| Field | Values |
|---|---|
| `extruder_index` | DEVICE numbering. Bambu dual-nozzle: `0` = main = RIGHT, `1` = deputy = LEFT. Klipper/Prusa: the tool number. |
| `position` | `right` / `left` on a Bambu dual-nozzle printer, otherwise `T0`, `T1`, ... (display only) |
| `nozzle_type` | `hardened_steel`, `stainless_steel`, `tungsten_carbide`, `brass`, `E3D`, `undefine` |
| `volume_type` | `Standard`, `High Flow`, `TPU High Flow`, `E3D High Flow`, `Hybrid`, `Extra High Flow` |

A slot is `(ams_id, tray_id)`: `0`-`7` AMS units, `128`+ AMS HT, `255` external.
External trays keep the engine's existing numbering: tray `0` is virtual tray
254 and tray `1` is virtual tray 255. On a dual-nozzle Bambu, vt 255 feeds
extruder 0 (right) and vt 254 feeds extruder 1 (left), so external tray 0 is
"External left" and tray 1 is "External right". A single-nozzle printer has one
external holder, tray 0.

### Bambu nozzle type codes

H2-series printers report `device.nozzle.info[].type` as a short code such as
`HS01`. The first character is the flow family (`S`, `A`, `X` Standard; `H`,
`E` High Flow; `U` TPU High Flow; `B` E3D High Flow) and the trailing two digits
the material (`00` stainless steel, `01` hardened steel, `05` tungsten carbide).
`N/A` means unset. Older firmware sends the long names (`hardened_steel`) in
`nozzle_type`; both are decoded, and the original string is kept as `raw_type`.

## Nozzles

Table `printer_nozzles` holds one row per `(printer_id, extruder_index)` with
`source` `mqtt` or `manual`.

- `GET /printers/{id}/nozzles` returns
  `{"nozzle_count", "nozzles": [{"extruder_index", "position", "diameter_mm", "nozzle_type", "volume_type", "source", "live", "active"}], "rack": [...]}`.
  The merged value comes from the manual row if there is one, else the live MQTT
  report, else the last stored MQTT row, else defaults (0.4 mm, `undefine`,
  `Standard`; `source: "default"`). `live` is the decoded live report
  (`diameter_mm`, `nozzle_type`, `volume_type`, `raw_type`) or null, and is shown
  even when a manual override wins. `rack` lists H2C rack docks decoded the same
  way, plus `id`, `wear`, `stat`, `serial_number`.
- `PUT /printers/{id}/nozzles/{extruder_index}` with
  `{"diameter_mm", "nozzle_type", "volume_type"}` stores a manual override
  (422 on an unknown type). Returns the merged view.
- `DELETE /printers/{id}/nozzles/{extruder_index}` drops the override (404 if
  there is none). Returns the merged view.

The status-change callback persists decoded live values as `source='mqtt'`
whenever they change. It keeps an in-memory snapshot, so the hot path is a dict
comparison, and it never overwrites a manual row.

## Filament paths

Which extruders a slot can feed.

- `GET /printers/{id}/filament-paths` returns
  `{"nozzle_count", "track_switch": {"installed", "source", "mode", "inlets": {"A", "B"}}, "slots": [{"ams_id", "tray_id", "label", "ams_type", "extruders", "fixed", "source"}]}`.
  There is one slot per AMS tray the printer reports, per manual row, and per
  external holder. For each slot, in order:
  1. a manual row for the tray, or for the whole unit (`tray_id` null);
  2. external holders feed their own extruder (see above);
  3. with a Filament Track Switch installed, every AMS slot feeds `[0, 1]`;
  4. the AMS unit's extruder from `ams_extruder_map` (AMS info bits 8-11);
  5. defaults: `[0]` on a single-nozzle printer, every extruder otherwise.
  `fixed` is true when the slot can feed exactly one extruder.
- `PUT /printers/{id}/filament-paths` with
  `{"track_switch_mode": "auto" | "installed" | "absent", "slots": [{"ams_id", "tray_id" | null, "extruders": [...]}]}`.
  `slots`, when present, replaces every manual row (an empty list clears them);
  leave it out to change only the mode. `auto` follows `fila_switch` from MQTT,
  the other two force it. Manual rows are how Klipper toolchangers and a Prusa
  XL describe their paths.

`track_switch.inlets` is a best-effort reading of `fila_switch.in` (the slot
loaded on each track) converted to an AMS unit id.

## Slot assignments carry an extruder

`spool_assignment` and `spoolman_slot_assignments` gain a nullable
`extruder_index`. `POST /inventory/assignments` and
`POST /spoolman/inventory/slot-assignments` accept an optional
`extruder_index`:

- It must be one of the slot's path extruders, otherwise the answer is
  `409 {"detail": "...", "allowed": [...]}`.
- When none is given and the slot feeds exactly one extruder, that one is
  stored; when the slot can feed several it stays null.
- Both responses, `GET /inventory/assignments` and
  `GET /spoolman/inventory/slot-assignments/all` include `extruder_index`. The
  Spoolman assign response also has `slot_location_id`.

When the slot has a bound location row (see sync-printers below), assigning
also moves the spool there: `location_id` in the local inventory, the Spoolman
`location` text in Spoolman mode. Un-assigning leaves the location alone.

## What is loaded

`GET /printers/{id}/loaded` is the "what is installed" view for slicing and for
clients:

```json
{
  "printer_id": 3, "printer_name": "H2D", "nozzle_count": 2, "active_extruder": 0,
  "track_switch": {"installed": false, "source": "mqtt", "mode": "auto", "inlets": {"A": null, "B": null}},
  "slots": [
    {"ams_id": 0, "tray_id": 1, "label": "AMS A slot 2", "ams_type": "AMS",
     "extruders": [0], "fixed": true, "source": "mqtt", "extruder_index": 0,
     "assignment": {"source": "internal", "spool_id": 12, "extruder_index": 0,
                    "material": "PLA", "color": "#FF0000", "brand": "Acme", "name": "Red",
                    "remaining_g": 640.0, "slicer_preset": "GFL99", "slicer_preset_name": "Generic PLA"},
     "live": {"tray_type": "PLA", "tray_color": "FF0000FF", "tray_sub_brands": "PLA Basic",
              "tray_info_idx": "GFL99", "remain": 64, "state": 11}}
  ]
}
```

`assignment.source` is `internal` or `spoolman` (the active inventory mode).
Assignments to slots the printer does not currently report are still listed,
with `live: null`.

## Locations

`locations` gains `kind` (`storage`, `drybox`, `printer`, `ams`, `ams_slot`,
`external`, `other`; default `storage`), `parent_id` (self reference, set to
null when the parent is deleted), `printer_id`, `ams_id`, `tray_id`, `code` and
`note`. The old free-form `identifier` is kept.

`code` is `L` followed by six Crockford base32 characters (no I, L, O or U), for
example `L7K2QX`. It is generated on insert, backfilled for existing rows by the
migration, unique, and never changes, so a reprinted label keeps working.
Lookups are case-insensitive and fold I/L to 1 and O to 0 after the prefix.

- `GET /inventory/locations` returns the new fields; filter with `?kind=` and
  `?printer_id=`.
- `POST` / `PATCH /inventory/locations` accept the new fields. PATCH applies only
  the fields present in the body (`{"parent_id": null}` clears the parent). A
  parent that does not exist or would make a cycle is a 422; a code already in
  use is a 409.
- `GET /inventory/locations/by-code/{code}` returns the location plus `spools`
  (internal spool ids, or Spoolman spool ids in Spoolman mode; `spool_source`
  says which) and `printer_name`.
- `POST /inventory/locations/sync-printers` creates, idempotently, one `printer`
  location per printer, one `ams` per AMS unit seen, one `ams_slot` per tray and
  the `external` holders, with parents linked. Rows are matched on
  `(kind, printer_id, ams_id, tray_id)`, so a second run only adds what is new.
  Names are `<printer>`, `<printer> - AMS A`, `<printer> - AMS A slot 2`,
  `<printer> - HT 1`, `<printer> - External left`. Returns every printer-bound
  location.

## Labels

- `POST /inventory/labels/locations` with
  `{"location_ids": [...], "template": "avery5160" | "62x29" | "62x100", "base_url": "https://hub.example"}`
  returns a PDF. The QR encodes `{base_url}/l/{code}`; the text shows the name,
  the kind (and the printer for AMS units, slots and external holders) and the
  code. Without `base_url` the server's own external URL is used.
- The spool label routes (`POST /inventory/labels`, `POST /spoolman/labels`)
  accept `"code_style": "hub"` with a `base_url`; the QR then encodes
  `{base_url}/l/S{spool id}` (the Spoolman id in Spoolman mode).
