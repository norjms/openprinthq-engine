"""Read the filament list a slicer wrote into a plain G-code file.

Added by OpenPrintHQ, AGPL-3.0. Used by the slot-by-slot remap (#52) for
non-Bambu multi-material printers, whose jobs are plain ``.gcode`` (the 3MF
reader in ``filament_requirements`` only covers Bambu ``.gcode.3mf``).

OrcaSlicer, PrusaSlicer and their forks (Creality Print, ElegooSlicer,
Anycubic Slicer Next, QIDI Studio) write the full config as ``; key = value``
comments, normally in a block at the end of the file and sometimes in the
header. Per-filament values are separated by ``;`` (some writers use ``,``):

    ; filament_type = PLA;PETG
    ; filament_colour = #FFFFFF;#1A1A1A
    ; filament used [g] = 12.30, 0.00

Only the first and last part of the file is read, so a 200 MB G-code costs a
few hundred kilobytes of I/O.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEAD_BYTES = 128 * 1024
_TAIL_BYTES = 512 * 1024

_LINE = re.compile(r"^;\s*([A-Za-z_][A-Za-z0-9_ \[\]]*?)\s*=\s*(.*?)\s*$")

_TYPE_KEYS = ("filament_type",)
_COLOUR_KEYS = ("filament_colour", "filament_color", "extruder_colour")
_USED_KEYS = ("filament used [g]", "filament used [mm]", "filament_used_g", "filament_used_mm")


def _split(value: str) -> list[str]:
    sep = ";" if ";" in value else ","
    return [v.strip().strip('"') for v in value.split(sep)]


def parse_config_lines(text: str) -> dict[str, str]:
    """Collect ``; key = value`` comment lines (last occurrence wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if m:
            out[m.group(1).strip().lower()] = m.group(2)
    return out


def filaments_from_config(config: dict[str, str]) -> list[dict]:
    """Build ``[{"tool", "type", "color", "used"}]`` from parsed config keys."""
    types: list[str] = []
    for key in _TYPE_KEYS:
        if key in config:
            types = _split(config[key])
            break
    if not types or not any(types):
        return []

    colours: list[str] = []
    for key in _COLOUR_KEYS:
        if key in config and any(_split(config[key])):
            colours = _split(config[key])
            break

    used: list[bool] | None = None
    for key in _USED_KEYS:
        if key in config:
            values = []
            for v in _split(config[key]):
                try:
                    values.append(float(v) > 0)
                except ValueError:
                    values.append(True)
            used = values
            break

    from backend.app.services.material_slots import normalize_hex

    out = []
    for i, ftype in enumerate(types):
        colour = normalize_hex(colours[i]) if i < len(colours) else None
        is_used = True if used is None or i >= len(used) else used[i]
        out.append({"tool": i, "type": ftype.upper(), "color": colour, "used": is_used})
    return out


def parse_gcode_filaments(path: str | Path) -> list[dict]:
    """Return the G-code's filaments in tool order, or [] when none are declared."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
            tail = b""
            if size > _HEAD_BYTES:
                fh.seek(max(_HEAD_BYTES, size - _TAIL_BYTES))
                tail = fh.read()
    except OSError:
        return []
    config = parse_config_lines(head.decode("utf-8", errors="replace"))
    config.update(parse_config_lines(tail.decode("utf-8", errors="replace")))
    return filaments_from_config(config)
