"""Farm-aware hardware matching for queue dispatch.

A job aimed at a printer model or a printer group can run on any idle member, so the
scheduler has to know whether a candidate's hardware can actually print the file:

* nozzle diameter: the size the file was sliced for must be installed;
* nozzle material: abrasive filament (carbon or glass fibre filled) needs a hardened
  nozzle, or it wears through a brass or stainless one in a few hours;
* build plate: the plate type the file was sliced for must match the plate the operator
  says is on the printer (``Printer.installed_plate``).

Every check is fail-safe: it only rejects a printer on a POSITIVE mismatch. Missing data
(no sliced nozzle in the file, a printer that has not reported its nozzles, no plate set
on the printer) never blocks a job, so a farm that does not record this information
behaves exactly as before.
"""

from __future__ import annotations

import re

from backend.app.services.printer_hardware import decode_bambu_nozzle_type

# Nozzle materials that tolerate abrasive filament, as named by
# printer_hardware.NOZZLE_TYPES. Bambu Studio rates each material's hardness
# (resources/info/nozzle_info.json: hardened steel 55 HRC, tungsten carbide 85,
# E3D 55, stainless steel 20, brass 2) and its CF/GF filament profiles require
# 40 HRC (required_nozzle_HRC), so stainless steel and brass do not qualify.
HARDENED_NOZZLES = frozenset({"hardened_steel", "tungsten_carbide", "E3D"})

_ABRASIVE = re.compile(r"(^|[^A-Z])(CF|GF)([^A-Z]|$)")


def is_abrasive(filament_type: str | None) -> bool:
    """True for carbon- or glass-fibre filled types: PLA-CF, PA6-GF, PETG CF, PAHT-CF, ..."""
    if not filament_type:
        return False
    return bool(_ABRASIVE.search(filament_type.upper()))


def normalize_plate(name: str | None) -> str:
    """Comparable key for a build plate name.

    The slicer writes e.g. "Textured PEI Plate" or "textured_pei_plate"; operators type
    "textured pei". All of them become "texturedpei".
    """
    if not name:
        return ""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    key = key[:-5] if key.endswith("plate") else key
    return _PLATE_ALIASES.get(key, key)


# Same plate, different slicer spellings. Bambu Studio writes "Cool Plate (SuperTack)",
# OrcaSlicer "Supertack Plate"; both are Bambu's SuperTack cool plate.
_PLATE_ALIASES = {
    "coolplatesupertack": "supertack",
}


def _nozzle_entry(raw_diameter, raw_material) -> tuple[float | None, str]:
    try:
        dia = float(raw_diameter or "")
    except (TypeError, ValueError):
        dia = None
    # The same decoding as the nozzle view: an H2 compact code ("HS01") or a long name
    # ("hardened_steel") becomes the material name; anything undecodable, and the
    # hardware model's "undefine", is unknown ("") and never fails a check.
    decoded = decode_bambu_nozzle_type(raw_material)
    material = decoded[0] if decoded else ""
    if material == "undefine":
        material = ""
    return (dia if dia and dia > 0 else None), material


def installed_nozzles(status, manual: dict | None = None) -> list[tuple[float | None, str]]:
    """(diameter, material) per nozzle; empty when unknown.

    ``status.nozzles`` is what the printer reported, indexed by extruder. ``manual`` maps
    an extruder index to a ``(diameter_mm, nozzle_type)`` override someone set with
    ``PUT /printers/{id}/nozzles/{extruder_index}`` (``printer_hardware.manual_nozzles``);
    an override wins over the report for that extruder, as it does in the nozzle view.
    Printers that report no nozzles (PrusaLink, OctoPrint, most Klipper setups) get
    their size from the override alone.
    """
    by_index: dict[int, tuple[float | None, str]] = {}
    for idx, nozzle in enumerate(getattr(status, "nozzles", None) or []):
        by_index[idx] = _nozzle_entry(getattr(nozzle, "nozzle_diameter", ""), getattr(nozzle, "nozzle_type", ""))
    for idx, (dia, material) in (manual or {}).items():
        by_index[int(idx)] = _nozzle_entry(dia, material)
    return [entry for _, entry in sorted(by_index.items()) if entry[0] or entry[1]]


def job_hardware(item, filament_types: list[str] | None = None) -> dict:
    """What a queue item needs from the printer, from its archive or library file metadata."""
    nozzle = None
    bed = None
    archive = getattr(item, "archive", None)
    library_file = getattr(item, "library_file", None)
    if archive is not None:
        nozzle = getattr(archive, "nozzle_diameter", None)
        bed = getattr(archive, "bed_type", None)
    if (nozzle is None or bed is None) and library_file is not None:
        meta = getattr(library_file, "file_metadata", None) or {}
        if nozzle is None:
            nozzle = meta.get("nozzle_diameter")
        if bed is None:
            bed = meta.get("bed_type")
    try:
        nozzle = float(nozzle) if nozzle not in (None, "") else None
    except (TypeError, ValueError):
        nozzle = None
    abrasive = sorted({t for t in (filament_types or []) if is_abrasive(t)})
    return {"nozzle_diameter": nozzle, "bed_type": bed or None, "abrasive": abrasive}


def hardware_mismatch(
    job: dict | None, status, installed_plate: str | None, manual_nozzles: dict | None = None
) -> list[str]:
    """Why this printer cannot print the job, as short "needs ..." phrases. Empty = fine.

    ``manual_nozzles`` are the printer's nozzle overrides (see ``installed_nozzles``)."""
    if not job:
        return []
    reasons: list[str] = []
    nozzles = installed_nozzles(status, manual_nozzles)

    want = job.get("nozzle_diameter")
    diameters = [d for d, _ in nozzles if d]
    if want and diameters and not any(abs(d - want) < 0.05 for d in diameters):
        reasons.append(f"a {want:g}mm nozzle (has {' / '.join(f'{d:g}mm' for d in diameters)})")

    abrasive = job.get("abrasive") or []
    materials = [m for _, m in nozzles if m]
    if abrasive and materials and not any(m in HARDENED_NOZZLES for m in materials):
        reasons.append(f"a hardened nozzle for {', '.join(abrasive)} (has {' / '.join(sorted(set(materials)))})")

    bed = job.get("bed_type")
    if bed and installed_plate and normalize_plate(bed) != normalize_plate(installed_plate):
        reasons.append(f"the {bed} (has {installed_plate})")
    return reasons
