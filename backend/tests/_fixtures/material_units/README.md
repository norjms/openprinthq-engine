# Multi-material unit payloads

Protocol payloads used as test fixtures for the CFS, QIDI Box, ACE and CANVAS
parsers. None of them was captured by OpenPrintHQ: we own none of these
printers. Each file records where its values come from. The values are
protocol data (field names, codes, sample readings) quoted from published
documentation and test suites, not code.

| File | Source | Licence of the source |
|------|--------|-----------------------|
| `cfs_k2plus_box_stock.json` | Shape and sample values of the live K2 Plus `box` frame quoted in HelixScreen `docs/devel/printer-research/CREALITY_CFS_K2_INTERNALS.md` (per-unit arrays, `same_material` group, RFID records); slot contents varied for the tests. github.com/prestonbrown/helixscreen | GPL-3.0 |
| `cfs_box_flat.json` | Flat (community Kalico) `box` schema, field table in the same HelixScreen document | GPL-3.0 |
| `qidi_save_variables.json`, `qidi_officiall_filas_list.cfg` | QIDI Box `save_variables` keys and the "trimmed-but-real excerpt" of the stock `officiall_filas_list.cfg` in HelixScreen `docs/devel/FILAMENT_BACKEND_QIDI_BOX.md` and `tests/unit/test_ams_backend_qidi.cpp` | GPL-3.0 |
| `orca_multicolour_tail.gcode` | Config block format written by OrcaSlicer-family slicers; values made up for the test | n/a |

GPL-3.0 is compatible with this project's AGPL-3.0 (section 13 of each).
