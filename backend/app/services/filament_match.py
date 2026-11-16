"""Filament matching: does what a printer has loaded fit what a job was sliced with (OpenPrintHQ farm feature).

See docs/filament-matching.md. Each slot of a job is matched on its own:

- Material: the loaded filament's material group must be the slot's (PLA Basic, PLA+ and PLA are one group;
  PLA-CF and PETG are others). A finish (Matte, Silk, ...) counts only when both sides name one.
- Colour, at the job's level:
    exact   the same filament (same Bambu filament id), or delta E 2 or less
    close   delta E 6 or less: looks the same at arm's length (the default)
    family  the same colour family (black, white, grey, red, ...)
    any     material only
- A saved mapping ("use this loaded filament for that requested one") counts as exact.

Colour difference is CIEDE2000 (delta E), the standard measure of how different two colours look.
Pure functions only; the scheduler and the routes supply the data.
"""

from __future__ import annotations

import colorsys
import math
import re
from dataclasses import asdict, dataclass, field

LEVELS = ("exact", "close", "family", "any")
DEFAULT_LEVEL = "close"
EXACT_DE = 2.0
CLOSE_DE = 6.0

# Material groups: names a slicer or a printer uses for one material. Editable through the
# filament_material_groups setting ({"PLA PRO": "PLA"}); these are the defaults.
DEFAULT_GROUPS: dict[str, str] = {
    "PLA": "PLA",
    "PLA BASIC": "PLA",
    "PLA+": "PLA",
    "PLA PLUS": "PLA",
    "PLA PRO": "PLA",
    "PLA-S": "PLA",
    "PLA HF": "PLA",
    "HYPER PLA": "PLA",
    "PLA TOUGH": "PLA",
    "PLA TOUGH+": "PLA",
    "PETG": "PETG",
    "PETG HF": "PETG",
    "PETG BASIC": "PETG",
    "PETG+": "PETG",
    "PET-G": "PETG",
    "ABS": "ABS",
    "ABS+": "ABS",
    "ASA": "ASA",
    "TPU": "TPU",
    "TPU 95A": "TPU",
    "TPU 90A": "TPU",
    "TPU 85A": "TPU",
    "TPU-AMS": "TPU",
    "TPU 95A HF": "TPU",
    "PC": "PC",
    "PA": "PA",
    "PA6": "PA",
    "PA12": "PA",
    "PAHT": "PA",
    "NYLON": "PA",
    "PA-CF": "PA-CF",
    "PA12-CF": "PA-CF",
    "PAHT-CF": "PA-CF",
    "PA6-CF": "PA-CF",
    "PVA": "PVA",
    "HIPS": "HIPS",
    "PET": "PET",
}
FINISHES = ("MATTE", "SILK", "GALAXY", "MARBLE", "WOOD", "METAL", "SPARKLE", "GLOW", "TRANSLUCENT", "GRADIENT")
_FILL = re.compile(r"[-\s]?(CF|GF)\b")
FAMILIES = ("black", "white", "grey", "red", "orange", "yellow", "green", "blue", "purple", "pink", "brown")


# ---------------------------------------------------------------- colour


def norm_hex(h: str | None) -> str | None:
    """#RRGGBB from 'RRGGBB', '#RRGGBBAA' and the like; None when it isn't a colour."""
    if not h:
        return None
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?", str(h).strip())
    return "#" + m.group(1).upper() if m else None


def _lab(hexc: str) -> tuple[float, float, float]:
    h = hexc.lstrip("#")[:6]
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in rgb]
    x = (lin[0] * 0.4124 + lin[1] * 0.3576 + lin[2] * 0.1805) / 0.95047
    y = lin[0] * 0.2126 + lin[1] * 0.7152 + lin[2] * 0.0722
    z = (lin[0] * 0.0193 + lin[1] * 0.1192 + lin[2] * 0.9505) / 1.08883
    f = [t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in (x, y, z)]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def delta_e(h1: str, h2: str) -> float:
    """CIEDE2000 difference of two #RRGGBB colours (about 1 = just noticeable, 10 or more = clearly different)."""
    L1, a1, b1 = _lab(h1)
    L2, a2, b2 = _lab(h2)
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb**7 / (Cb**7 + 25**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dL, dC = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dh = 0.0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    else:
        dh = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dH = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dh / 2))
    Lb, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    if C1p * C2p == 0:
        hb = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hb = (h1p + h2p) / 2
    else:
        hb = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    T = (
        1
        - 0.17 * math.cos(math.radians(hb - 30))
        + 0.24 * math.cos(math.radians(2 * hb))
        + 0.32 * math.cos(math.radians(3 * hb + 6))
        - 0.20 * math.cos(math.radians(4 * hb - 63))
    )
    SL = 1 + 0.015 * (Lb - 50) ** 2 / math.sqrt(20 + (Lb - 50) ** 2)
    SC, SH = 1 + 0.045 * Cbp, 1 + 0.015 * Cbp * T
    RT = -2 * math.sqrt(Cbp**7 / (Cbp**7 + 25**7)) * math.sin(math.radians(60 * math.exp(-(((hb - 275) / 25) ** 2))))
    return math.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))


def family(hexc: str | None) -> str | None:
    """The colour family a #RRGGBB colour belongs to: greys by lightness, the rest by hue, saturation and value."""
    h = norm_hex(hexc)
    if not h:
        return None
    L, a, b = _lab(h)
    if math.hypot(a, b) < 10 or (L > 85 and math.hypot(a, b) < 22):
        return "black" if L < 25 else "white" if L > 85 else "grey"
    r, g, bl = (int(h[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, sat, val = colorsys.rgb_to_hsv(r, g, bl)
    hue *= 360
    if 15 <= hue < 50 and val < 0.6:
        return "brown"
    if 20 <= hue < 45 and sat < 0.45 and val < 0.85:
        return "brown"
    if hue < 12 or hue >= 340:
        return "pink" if sat < 0.5 or (val > 0.85 and sat < 0.7) else "red"
    if hue < 45:
        return "orange"
    if hue < 70:
        return "yellow"
    if hue < 170:
        return "green"
    if hue < 245:
        return "blue"
    if hue < 320:
        return "purple"
    return "pink"


# ---------------------------------------------------------------- material


def material(ftype: str | None, groups: dict[str, str] | None = None) -> tuple[str, str]:
    """(group, finish) of a filament type such as 'PLA Matte', 'PETG HF' or 'PLA-CF'."""
    t = re.sub(r"\s+", " ", str(ftype or "").upper().replace("_", " ")).strip()
    finish = next((f for f in FINISHES if re.search(rf"\b{f}\b", t)), "")
    if finish:
        t = re.sub(rf"\s*\b{finish}\b\s*", " ", t).strip()
    table = {**DEFAULT_GROUPS, **{k.upper(): v.upper() for k, v in (groups or {}).items()}}
    if t in table:
        return table[t], finish
    fill = _FILL.search(t)
    base = _FILL.sub("", t).strip(" -")
    group = table.get(base, base)
    return (f"{group}-{fill.group(1)}" if fill else group), finish


# ---------------------------------------------------------------- matching


@dataclass
class Verdict:
    ok: bool
    grade: str  # same | close | family | material | different_colour | different_material | none
    delta_e: float | None = None
    note: str = ""


def _signature(ftype: str | None, color: str | None) -> str:
    return f"{(ftype or '').strip().upper()}|{norm_hex(color) or ''}"


def compare(req: dict, tray: dict, level: str, groups: dict | None = None, aliases: set | None = None) -> Verdict:
    """How a loaded tray fits one required slot at a level."""
    rg, rf = material(req.get("type"), groups)
    tg, tf = material(tray.get("type"), groups)
    if (
        aliases
        and (_signature(req.get("type"), req.get("color")), _signature(tray.get("type"), tray.get("color"))) in aliases
    ):
        return Verdict(True, "same", 0.0, "saved mapping")
    if rg != tg:
        return Verdict(
            False, "different_material", None, f"needs {req.get('type') or rg}, has {tray.get('type') or tg}"
        )
    rc, tc = norm_hex(req.get("color")), norm_hex(tray.get("color"))
    de = round(delta_e(rc, tc), 1) if rc and tc else None
    finish_differs = bool(rf and tf and rf != tf)
    same_id = bool(req.get("tray_info_idx")) and req.get("tray_info_idx") == tray.get("tray_info_idx")
    if level == "any":
        return Verdict(True, "material", de)
    if finish_differs:
        return Verdict(False, "different_material", de, f"needs {rf.title()}, has {tf.title()}")
    if de is None:
        # A colour we can't read on either side never blocks a match on material.
        return Verdict(True, "material", None, "colour unknown")
    if same_id and de <= CLOSE_DE or de <= EXACT_DE:
        return Verdict(True, "same", de)
    if de <= CLOSE_DE:
        return Verdict(level in ("close", "family"), "close", de)
    if family(rc) == family(tc):
        return Verdict(level == "family", "family", de)
    return Verdict(False, "different_colour", de)


@dataclass
class SlotResult:
    slot_id: int
    type: str
    color: str
    grams: float | None
    ok: bool
    grade: str
    delta_e: float | None
    tray: dict | None = None
    note: str = ""


@dataclass
class MatchResult:
    ok: bool
    level: str
    slots: list[SlotResult] = field(default_factory=list)
    mapping: list[int] = field(default_factory=list)  # Bambu ams_mapping: index = slot_id - 1, value = global tray id

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        bad = [s for s in self.slots if not s.ok]
        parts = []
        for s in bad:
            have = s.tray and f"{s.tray.get('type')} {s.tray.get('color')}"
            gap = f", delta E {s.delta_e}" if s.delta_e is not None else ""
            parts.append(f"filament {s.slot_id} {s.type} {s.color} (closest: {have or 'nothing loaded'}{gap})")
        return "; ".join(parts)


_RANK = {"same": 0, "close": 1, "family": 2, "material": 3, "different_colour": 4, "different_material": 5}


def match(
    required: list[dict],
    loaded: list[dict],
    level: str = DEFAULT_LEVEL,
    groups: dict | None = None,
    aliases: set | None = None,
    fts_installed: bool = False,
) -> MatchResult:
    """Match each required slot to the best loaded tray, each tray used once.

    required: [{slot_id, type, color, tray_info_idx?, used_grams?, nozzle_id?}] (filament_requirements)
    loaded:   [{type, color, tray_info_idx, global_tray_id, extruder_id?, remain?}] (the scheduler's loaded list)
    """
    level = level if level in LEVELS else DEFAULT_LEVEL
    used: set[int] = set()
    out: list[SlotResult] = []
    for req in sorted(required, key=lambda r: int(r.get("slot_id") or 0)):
        pool = [t for t in loaded if t.get("global_tray_id") not in used]
        nozzle = req.get("nozzle_id")
        if nozzle is not None and not fts_installed:
            pool = [t for t in pool if t.get("extruder_id") in (None, nozzle)]
        best: tuple | None = None
        for t in pool:
            v = compare(req, t, level, groups, aliases)
            key = (not v.ok, _RANK.get(v.grade, 9), v.delta_e if v.delta_e is not None else 99.0)
            if best is None or key < best[0]:
                best = (key, t, v)
        if best is None:
            out.append(
                SlotResult(
                    int(req.get("slot_id") or 0),
                    req.get("type") or "",
                    norm_hex(req.get("color")) or "",
                    req.get("used_grams"),
                    False,
                    "none",
                    None,
                    None,
                    "nothing loaded",
                )
            )
            continue
        _, tray, v = best
        if v.ok:
            used.add(tray.get("global_tray_id"))
        out.append(
            SlotResult(
                int(req.get("slot_id") or 0),
                req.get("type") or "",
                norm_hex(req.get("color")) or "",
                req.get("used_grams"),
                v.ok,
                v.grade,
                v.delta_e,
                _tray_view(tray),
                v.note,
            )
        )
    mapping: list[int] = []
    if out:
        mapping = [-1] * max(s.slot_id for s in out)
        for s in out:
            if s.ok and s.tray and s.slot_id >= 1:
                mapping[s.slot_id - 1] = s.tray["global_tray_id"]
    return MatchResult(all(s.ok for s in out), level, out, mapping)


def _tray_view(t: dict) -> dict:
    return {
        "global_tray_id": t.get("global_tray_id"),
        "type": t.get("type"),
        "color": norm_hex(t.get("color")),
        "tray_info_idx": t.get("tray_info_idx") or None,
        "remain": t.get("remain"),
        "extruder_id": t.get("extruder_id"),
        "label": tray_label(t),
    }


def tray_label(t: dict) -> str:
    """'AMS A slot 2', 'AMS HT 1', 'External spool' for a loaded tray."""
    gid = t.get("global_tray_id")
    if t.get("is_external") or (isinstance(gid, int) and gid >= 254):
        return "External spool" if gid in (254, None) else "External spool (left)" if gid == 255 else "External spool"
    if isinstance(gid, int) and gid >= 128:
        return f"AMS HT {gid - 127}"
    if isinstance(gid, int):
        return f"AMS {chr(65 + gid // 4)} slot {gid % 4 + 1}"
    return "Slot"


def needs_reslice(req: dict, tray: dict, groups: dict | None = None) -> bool:
    """Whether printing a slot with this tray needs the file sliced again: another material group."""
    return material(req.get("type"), groups)[0] != material(tray.get("type"), groups)[0]


def alias_key(req_type: str | None, req_color: str | None, tray_type: str | None, tray_color: str | None) -> tuple:
    return (_signature(req_type, req_color), _signature(tray_type, tray_color))


def check_mapping(
    required: list[dict],
    loaded: list[dict],
    mapping: list[int] | None,
    level: str = DEFAULT_LEVEL,
    groups: dict | None = None,
    aliases: set | None = None,
) -> MatchResult | None:
    """Judge a given AMS mapping (index = slot_id - 1, value = global tray id) at a level.

    None when the mapping doesn't place every required slot on a loaded tray, so the caller falls back to match().
    """
    if not mapping:
        return None
    by_id = {t.get("global_tray_id"): t for t in loaded}
    out: list[SlotResult] = []
    for req in sorted(required, key=lambda r: int(r.get("slot_id") or 0)):
        sid = int(req.get("slot_id") or 0)
        gid = mapping[sid - 1] if 1 <= sid <= len(mapping) else None
        tray = by_id.get(gid)
        if tray is None:
            return None
        v = compare(req, tray, level if level in LEVELS else DEFAULT_LEVEL, groups, aliases)
        out.append(
            SlotResult(
                sid,
                req.get("type") or "",
                norm_hex(req.get("color")) or "",
                req.get("used_grams"),
                v.ok,
                v.grade,
                v.delta_e,
                _tray_view(tray),
                v.note,
            )
        )
    return MatchResult(all(s.ok for s in out), level, out, list(mapping))
