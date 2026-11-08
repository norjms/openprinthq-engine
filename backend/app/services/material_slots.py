"""Per-slot material tracking for non-Bambu multi-material units.

Added by OpenPrintHQ, AGPL-3.0. See docs/multi-material-units.md.

Creality CFS, QIDI Box, Anycubic ACE Pro and Elegoo CANVAS are all four-slot
feeders that can be chained, and every one of them reports, per slot, a
material type, a colour and whether a spool is present. Each printer client
parses its own protocol into the vendor-neutral ``MaterialUnit`` /
``MaterialSlot`` model below and hands it to ``apply_units_to_state``.

That function writes the units into ``PrinterState.raw_data["ams"]`` in the
same tray shape the Bambu client produces, with ``module_type`` naming the unit
kind. Everything that already reads AMS trays (the printer card, the farm
matcher's "missing filament" hold, the colour ranking, spool assignment, the
slicer pre-send check) therefore sees these slots without a second code path.
Bambu-only actions (RFID re-read, load/unload over MQTT, drying) stay gated on
the connection type, so they are never offered for these units.

Global slot numbering follows the Bambu convention ``unit_id * 4 + slot`` with
0-based units, which is also how every one of these vendors numbers a slot
across chained units (CFS ``(address - 1) * 4 + local``, QIDI ``slot0..15``,
ACE ``ams_index``, CANVAS ``canvas_id``/``tray_id``).

``remap_slots`` is the slot-by-slot colour remap (#52): every G-code filament
goes to a present slot of the same material type, choosing the closest colour.
Material type is the only hard constraint; colour only ranks, matching the
dispatcher, which holds a job on material type alone.

None of this has been verified on real hardware: the parsers are built from
published protocol documentation and captured payloads (sources are listed in
the docs and in each parser module).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

SLOTS_PER_UNIT = 4

UNIT_CFS = "cfs"  # Creality Filament System (K-series)
UNIT_QIDI_BOX = "qidi_box"  # QIDI Box (Plus4 / Q2 / Max4)
UNIT_ACE = "ace"  # Anycubic ACE Pro (Kobra 3 / S1)
UNIT_CANVAS = "canvas"  # Elegoo CANVAS (Centauri Carbon / Carbon 2)

UNIT_KINDS = frozenset({UNIT_CFS, UNIT_QIDI_BOX, UNIT_ACE, UNIT_CANVAS})


@dataclass
class MaterialSlot:
    """One feeder slot. ``index`` is the slot within its unit (0-3)."""

    index: int
    present: bool = False
    material: str | None = None  # e.g. "PLA", "PETG"; None when unknown
    colour: str | None = None  # "#RRGGBB" or None
    name: str | None = None  # product name, e.g. "Hyper PLA"
    brand: str | None = None
    remain_percent: int | None = None  # 0-100, None when the unit does not report it
    active: bool = False  # currently feeding the toolhead
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None


@dataclass
class MaterialUnit:
    """One multi-material unit. ``unit_id`` is 0-based."""

    kind: str
    unit_id: int
    slots: list[MaterialSlot] = field(default_factory=list)
    connected: bool = True
    humidity: float | None = None
    temperature: float | None = None
    serial: str | None = None
    firmware: str | None = None

    def global_slot(self, local_index: int) -> int:
        return self.unit_id * SLOTS_PER_UNIT + local_index


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
def normalize_hex(value) -> str | None:
    """Return ``#RRGGBB`` (upper case) for the colour spellings vendors use.

    Accepts ``#RRGGBB``, ``RRGGBB``, ``RRGGBBAA`` (alpha dropped), Creality's
    ``0RRGGBB`` (a pad digit then RGB), and ``[r, g, b]`` / ``[r, g, b, a]``
    lists (Anycubic). Anything else, including multi-colour spools written as
    ``"#aaaaaa,#bbbbbb"``, returns None.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if len(value) < 3:
            return None
        try:
            r, g, b = (max(0, min(255, int(v))) for v in value[:3])
        except (TypeError, ValueError):
            return None
        return f"#{r:02X}{g:02X}{b:02X}"
    text = str(value).strip()
    if not text or "," in text or ";" in text:
        return None
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 7 and text[0] == "0":
        text = text[1:]  # Creality CFS "0RRGGBB"
    if len(text) == 8:
        text = text[:6]  # RRGGBBAA
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return f"#{text.upper()}"


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def colour_distance(a: str | None, b: str | None) -> float:
    """Perceptual-ish RGB distance (redmean). Unknown colours sort last."""
    na, nb = normalize_hex(a), normalize_hex(b)
    if na is None or nb is None:
        return 1000.0
    r1, g1, b1 = _rgb(na)
    r2, g2, b2 = _rgb(nb)
    rmean = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return ((2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db) ** 0.5


def canonical_material(material: str | None) -> str:
    """Upper-case material type with the scheduler's equivalence groups applied.

    The same canonicalisation the dispatcher's "missing filament" hold uses, so
    the remap never picks a slot the hold would not accept.
    """
    if not material:
        return ""
    from backend.app.services.print_scheduler import _canonical_filament_type

    text = str(material).strip().upper()
    return _canonical_filament_type(text)


# --------------------------------------------------------------------------
# Bambu-shaped projection
# --------------------------------------------------------------------------
def _tray_dict(slot: MaterialSlot) -> dict:
    if not slot.present:
        # The printer card treats a tray carrying only its id as a
        # physically empty slot (see printer_state_to_dict).
        return {"id": slot.index}
    colour = normalize_hex(slot.colour)
    tray = {
        "id": slot.index,
        "tray_type": (slot.material or "").upper(),
        "tray_color": (colour.lstrip("#") + "FF") if colour else "",
        "tray_sub_brands": slot.name or "",
        "tray_id_name": slot.brand or "",
        "tray_info_idx": "",
        "remain": slot.remain_percent if slot.remain_percent is not None else -1,
        "state": 3,
    }
    if slot.nozzle_temp_min is not None:
        tray["nozzle_temp_min"] = str(slot.nozzle_temp_min)
    if slot.nozzle_temp_max is not None:
        tray["nozzle_temp_max"] = str(slot.nozzle_temp_max)
    return tray


def units_to_ams(units: list[MaterialUnit]) -> list[dict]:
    """Project units onto the Bambu ``raw_data["ams"]`` list shape."""
    out: list[dict] = []
    for unit in sorted(units, key=lambda u: u.unit_id):
        by_index = {s.index: s for s in unit.slots}
        trays = [_tray_dict(by_index.get(i, MaterialSlot(index=i))) for i in range(SLOTS_PER_UNIT)]
        entry: dict = {
            "id": unit.unit_id,
            "module_type": unit.kind,
            "tray": trays,
            "sn": unit.serial or "",
            "sw_ver": unit.firmware or "",
        }
        if unit.humidity is not None:
            entry["humidity_raw"] = int(round(unit.humidity))
        if unit.temperature is not None:
            entry["temp"] = round(float(unit.temperature), 1)
        out.append(entry)
    return out


def active_global_slot(units: list[MaterialUnit]) -> int | None:
    for unit in units:
        for slot in unit.slots:
            if slot.active and slot.present:
                return unit.global_slot(slot.index)
    return None


def apply_units_to_state(state, units: list[MaterialUnit]) -> None:
    """Write parsed units into a ``PrinterState`` (raw_data + tray_now)."""
    connected = [u for u in units if u.connected]
    if not connected:
        state.raw_data.pop("ams", None)
        state.raw_data.pop("material_units", None)
        state.tray_now = 255
        return
    state.raw_data["ams"] = units_to_ams(connected)
    state.raw_data["material_units"] = [asdict(u) for u in connected]
    active = active_global_slot(connected)
    state.tray_now = active if active is not None else 255


def units_from_state(state) -> list[MaterialUnit]:
    """Rebuild units from ``raw_data["material_units"]`` (empty if none)."""
    out: list[MaterialUnit] = []
    for raw in (getattr(state, "raw_data", None) or {}).get("material_units") or []:
        slots = [MaterialSlot(**s) for s in raw.get("slots") or []]
        data = {k: v for k, v in raw.items() if k != "slots"}
        out.append(MaterialUnit(slots=slots, **data))
    return out


# --------------------------------------------------------------------------
# Slot-by-slot remap (#52)
# --------------------------------------------------------------------------
def remap_slots(required: list[dict], units: list[MaterialUnit]) -> list[int]:
    """Map each G-code filament to a slot: same material, closest colour.

    ``required`` is a list of ``{"tool": int, "type": str, "color": str,
    "used": bool}`` in G-code tool order (as ``gcode_filaments`` returns).
    Returns a list the same length, one global slot index per entry, or -1
    where no present slot holds that material (or the filament is unused).

    A slot already given to another filament is avoided while an unused slot
    of the right material exists, and reused only as a last resort (two G-code
    filaments of the same material and colour may share one spool).
    """
    candidates: list[tuple[int, MaterialSlot]] = []
    for unit in units:
        if not unit.connected:
            continue
        for slot in unit.slots:
            if slot.present and slot.material:
                candidates.append((unit.global_slot(slot.index), slot))

    mapping: list[int] = []
    taken: set[int] = set()
    for req in required:
        if not req.get("used", True):
            mapping.append(-1)
            continue
        want = canonical_material(req.get("type"))
        same_type = [(g, s) for g, s in candidates if canonical_material(s.material) == want] if want else []
        if not same_type:
            mapping.append(-1)
            continue
        free = [(g, s) for g, s in same_type if g not in taken] or same_type
        best = min(free, key=lambda gs: (colour_distance(req.get("color"), gs[1].colour), gs[0]))
        mapping.append(best[0])
        taken.add(best[0])
    return mapping


def describe_mapping(required: list[dict], mapping: list[int]) -> list[dict]:
    """Human-readable pairs for logs and the queue item (tool -> slot)."""
    out = []
    for req, slot in zip(required, mapping, strict=False):
        out.append(
            {
                "tool": req.get("tool"),
                "type": req.get("type"),
                "color": req.get("color"),
                "slot": slot,
                "unit": slot // SLOTS_PER_UNIT if slot >= 0 else None,
                "unit_slot": slot % SLOTS_PER_UNIT if slot >= 0 else None,
            }
        )
    return out
