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

# Nozzle materials that tolerate abrasive filament. Bambu reports
# "hardened_steel" / "stainless_steel"; tungsten carbide shows up on newer hotends.
HARDENED_NOZZLES = frozenset({"hardened_steel", "tungsten_carbide", "hardened", "carbide"})

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


def installed_nozzles(status) -> list[tuple[float | None, str]]:
    """(diameter, material) per nozzle the printer can print with; empty when unknown.

    The mounted nozzles, plus a tool switcher's rack (the H2C swaps the right extruder's hotend from a rack of up
    to six): a job that needs a 0.6 prints when a 0.6 is mounted or sitting in the rack, because the printer swaps it
    in itself. Rack entries count only when a nozzle is really there (a diameter and a serial number).
    """
    out: list[tuple[float | None, str]] = []
    for nozzle in getattr(status, "nozzles", None) or []:
        raw = getattr(nozzle, "nozzle_diameter", "") or ""
        try:
            dia = float(raw)
        except (TypeError, ValueError):
            dia = None
        material = (getattr(nozzle, "nozzle_type", "") or "").strip().lower()
        if (dia and dia > 0) or material:
            out.append((dia if dia and dia > 0 else None, material))
    out += rack_nozzles(status)
    return out


def rack_nozzles(status) -> list[tuple[float | None, str]]:
    """(diameter, material) for each nozzle in a tool switcher's rack (Bambu nozzle_rack ids 16 and up)."""
    from backend.app.services.printer_hardware import decode_bambu_nozzle_type

    out: list[tuple[float | None, str]] = []
    for slot in getattr(status, "nozzle_rack", None) or []:
        get = slot.get if isinstance(slot, dict) else (lambda k, d=None, _s=slot: getattr(_s, k, d))
        try:
            if int(get("id", -1)) < 16:
                continue  # 0 and 1 describe the mounted hotends, already counted above
        except (TypeError, ValueError):
            continue
        serial = str(get("serial_number", "") or "").strip()
        try:
            dia = float(get("nozzle_diameter", 0) or 0)
        except (TypeError, ValueError):
            dia = 0.0
        if dia <= 0 or not serial or serial.upper() == "N/A":
            continue
        decoded = decode_bambu_nozzle_type(get("nozzle_type"))
        material = (decoded[0] if decoded else "").strip().lower()
        out.append((dia, material))
    return out


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


def hardware_mismatch(job: dict | None, status, installed_plate: str | None) -> list[str]:
    """Why this printer cannot print the job, as short "needs ..." phrases. Empty = fine."""
    if not job:
        return []
    reasons: list[str] = []
    nozzles = installed_nozzles(status)

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
