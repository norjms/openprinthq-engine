# Anycubic LAN-mode payloads

OpenPrintHQ owns no Anycubic printer. These reports were captured from a
Kobra S1 Max (firmware 2.6.9.6, ACE model 40002) by the anycubic_ha_local
project (github.com/chrisfore/anycubic_ha_local, `tests/fixtures/`, licensed
AGPL-3.0-or-later, the same licence as this project) and are used unchanged
except that the printer's LAN address was replaced with a documentation
address (192.0.2.50).

| File | Report type |
|------|-------------|
| `info_report.json` | `info` `.data` during a print |
| `multicolorbox_full.json` | `multiColorBox` `.data`, full box during a print |
| `multicolorbox_slim.json` | `multiColorBox` `.data`, a one-slot update |
| `print_progress.json` | `print` `.data` progress shape |
