# Multi-material units on non-Bambu printers

OpenPrintHQ tracks each slot of four non-Bambu multi-material units:

| Unit | Printers | Connection type in OpenPrintHQ |
|------|----------|--------------------------------|
| Creality Filament System (CFS) | K2, K2 Plus, K2 Pro, K1 / K1C / K1 Max with the CFS upgrade | `klipper` (Moonraker) |
| QIDI Box | QIDI Plus4, Q2, Max4 | `klipper` (Moonraker) |
| Elegoo CANVAS | Centauri Carbon (SDCP), Centauri Carbon 2 | `elegoo`, `elegoo_cc2` (see `docs/elegoo-printers.md`) |
| Anycubic ACE Pro | Kobra 3, Kobra 3 Max, Kobra 3 V2, Kobra S1, Kobra S1 Max | `anycubic` (see `docs/anycubic-printers.md`) |

> **Untested on hardware.** None of these units has been run against
> OpenPrintHQ: the project owns none of them. Everything below is built from
> published protocol documentation and captured payloads from other open-source
> projects (listed under Sources), and the payloads are used as test fixtures
> (`backend/tests/_fixtures/material_units/`). The printer card marks these
> units "Untested on hardware". Reports from owners, good or bad, are welcome.

## What you get

- Each slot's material type, colour, presence and (where the unit says) the
  product name, brand and nozzle temperature range, plus each unit's humidity
  and temperature.
- The slots show in the printer card's filament list like AMS trays, labelled
  by unit (CFS 1, QIDI Box 2). Spools can be assigned to them.
- The farm dispatcher sees them: a job aimed at a model or group waits only
  when no candidate has the material loaded (`missing filament: <type>`), and
  colour ranks candidates, exactly as for an AMS.
- When a plain G-code job starts, every filament the G-code uses is remapped
  to a slot (see Slot remap).

Bambu-only slot actions are not offered for these units: RFID re-read, load and
unload over MQTT, drying and slot presets. Loading and unloading is done on the
printer.

## How each unit is read

Global slot numbers are `unit * 4 + slot`, with units and slots counted from 0,
which is also how each vendor numbers slots across chained units.

### Creality CFS (Moonraker `box` object)

The engine detects the `box` object in Moonraker's object list and subscribes
to it. Creality's stock firmware publishes `T1`..`T4` (one per chained unit,
`state: "connect"` when present), each with four per-slot arrays:

| Field | Meaning |
|-------|---------|
| `vender` | `"none"` = empty bay, `"unknown"` = spool without an RFID tag, otherwise the tag record |
| `material_type` | Creality material code (`101001` Hyper PLA, `106002` Hyper PETG, `1000NN` generic) |
| `color_value` | `0RRGGBB` (a pad digit, then the colour) |
| `remain_len` | metres left (no percentage, so the fill bar stays empty) |

Codes the engine does not know are resolved through the box's own
`same_material` groups, which carry the material name, and generic codes by
their suffix. A spool whose material cannot be resolved still shows (colour,
presence) but satisfies no material requirement. Frames are deltas, so the
engine keeps a merged copy. Community Kalico ports that publish the flat schema
(`slots[]` with `material`, `color`, `present`, `loaded`) are read too; the
external spool entry is left out.

### QIDI Box (Moonraker `save_variables`)

Detected from the `box_stepper slot<N>` objects. The engine subscribes to
`save_variables`, `multi_color_controller` when present, and the per-box
`aht20_f heater_box<N>` sensors, and on connect downloads the printer's
`officiall_filas_list.cfg` from the Moonraker config root.

| Variable | Meaning |
|----------|---------|
| `box_count` | chained boxes (0-4) |
| `slot<N>` | 0 empty, 1 or 3 present, 2 loaded, negative blocked |
| `filament_slot<N>` | row of `[fila<N>]` in `officiall_filas_list.cfg` (material `type`, product name, nozzle range), or a product id such as `QFA90` on newer box firmware, then resolved from `multi_color_controller` |
| `color_slot<N>` | row of `[colordict]`, or a list such as `['228332']` on newer firmware |
| `vendor_slot<N>` | row of `[vendor_list]` |
| `value_t<N>` | `"slot<M>"`: G-code tool N prints from slot M |

## Slot remap (#52)

When the queue dispatches a plain `.gcode` file to a printer with one of these
units:

1. The engine reads the G-code's filament list from the slicer's config block
   (`; filament_type = PLA;PETG`, `; filament_colour = #FFFFFF;#1A1A1A`, and
   `; filament used [g]` to skip filaments the file never uses). OrcaSlicer,
   PrusaSlicer and their forks (Creality Print, QIDI Studio, ElegooSlicer,
   Anycubic Slicer Next) all write it.
2. The queue item's filament overrides replace the type and colour of the
   filaments they name.
3. Each filament goes to a present slot holding the same material (the same
   equivalence groups as the dispatcher's hold, so `PA12-CF` satisfies
   `PA-CF`), choosing the closest colour and avoiding slots already given to
   another filament. A filament whose material is not loaded gets no slot and
   keeps the printer's own default for that tool.
4. The remap is stored on the queue item as `ams_mapping` (one global slot per
   G-code filament, `-1` for none) and shown on the printer card's slots while
   the job runs, as the AMS mapping is for Bambu printers. A mapping already
   stored on the item is used as it is.

How the remap reaches each printer:

| Unit | How | Notes |
|------|-----|-------|
| CFS | `BOX_MODIFY_TN T<tool>=T<slot>` for every tool the job uses (slots named `T1A`..`T4D`), then `SDCARD_PRINT_FILE`, in one G-code script | The firmware keeps the table after the job, so every tool is written (identity for the ones that stay) to clear an earlier remap. A job later started from the printer's screen uses the last table written. |
| QIDI Box | `SAVE_VARIABLE VARIABLE=value_t<N> VALUE='"slot<M>"'` per tool, then `SDCARD_PRINT_FILE`, in one script | Same persistence note: `value_t<N>` is the box's own tool map. |
| ACE Pro | `ams_settings.ams_box_mapping` in the MQTT start command: `{"paint_index", "ams_index", "paint_color", "ams_color", "material_type"}` per remapped filament | Per job. Without matches no `ams_settings` is sent and the printer maps itself. |
| CANVAS | `slot_map` in the start command (SDCP Cmd 128, CC2 method 1020): `{"t", "canvas_id", "tray_id"}` per remapped tool | Per job, nothing persists. Tools without a tray are left out and the printer chooses. |

Material type is the only reason the dispatcher holds a job. Colour never holds
a job; it ranks printers and picks slots. Plain G-code jobs carry no
`required_filament_types` today (that list is read from 3MF files), so the
material hold does not apply to them yet; the remap then simply leaves a
filament whose material is missing on the printer's default slot.

## What to check on a real printer

- CFS: the card lists every bay with the right colour and material, empty bays
  as empty, and the unit's humidity; a job with two colours prints each from the
  slot the queue item's `ams_mapping` names; the box's `map` field (K2) shows
  the remap.
- QIDI Box: same, and `value_t<N>` in `save_variables` matches `ams_mapping`
  after dispatch.

### Elegoo CANVAS

Read with SDCP Cmd 324 or CC2 method 2005 (`canvas_info.canvas_list[].tray_list[]`:
`filament_type`, `filament_color`, `filament_name`, `brand`, nozzle range,
`status` 0 empty / 1 loaded / 2 feeding). Details in `docs/elegoo-printers.md`.

### Anycubic ACE Pro

`multiColorBox` reports (`multi_color_box[].slots[]`: `type`, `color` [r, g, b],
`status` 5 loaded / 4 ready, `consumables_percent`), merged by box and slot,
polled with `getInfo`. Details in `docs/anycubic-printers.md`.

## Sources

- HelixScreen (GPL-3.0), github.com/prestonbrown/helixscreen:
  `docs/devel/FILAMENT_BACKEND_CFS.md`,
  `docs/devel/printer-research/CREALITY_CFS_K2_INTERNALS.md` and
  `CREALITY_CFS_K1_INTERNALS.md` (the `box` object, material codes, colour
  encoding, `BOX_MODIFY_TN`), `docs/devel/FILAMENT_BACKEND_QIDI_BOX.md` and
  `tests/unit/test_ams_backend_qidi.cpp` (save_variables keys,
  `officiall_filas_list.cfg`, `multi_color_controller` keys, the
  `SAVE_VARIABLE` quoting).
- ha_creality_ws (AGPL-3.0), github.com/3dg1luk43/ha_creality_ws: the CFS
  `boxsInfo` / material model and its simulator, used to cross-check slot
  states and colour encoding.
- Elegoo CANVAS: the SDCP v3 specification, Elegoo's elegoo-link SDK
  (Apache-2.0) and elegoo-homeassistant (MIT); see `docs/elegoo-printers.md`.
- Anycubic ACE: kobra-connect (Apache-2.0), anycubic_ha_local (AGPL-3.0) and
  Rinkhals; see `docs/anycubic-printers.md`.
- QIDI's Klipper and Moonraker forks, github.com/QIDITECH, and the community
  Box firmware reimplementation in github.com/qidi-community/Plus4-Wiki.

No code was copied from these projects; they are cited for the protocol facts
and sample payloads. GPL-3.0 and AGPL-3.0 sources are compatible with this
project's AGPL-3.0.
