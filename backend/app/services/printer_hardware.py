"""Nozzle and filament-path model for printers.

Vocabulary follows OrcaSlicer / Bambu Studio:

- ``extruder_index`` is DEVICE numbering. Bambu dual-nozzle: 0 = main = RIGHT,
  1 = deputy = LEFT. Klipper/Prusa: the tool number.
- ``nozzle_type``: hardened_steel | stainless_steel | tungsten_carbide | brass | E3D | undefine.
- ``volume_type``: Standard | High Flow | TPU High Flow | E3D High Flow | Hybrid | Extra High Flow.

Slot identity is ``(ams_id, tray_id)``: 0-7 AMS units, 128+ AMS HT, 255 external.
External trays follow the engine's existing convention: tray 0 = vt 254, tray 1 =
vt 255. On a dual-nozzle Bambu vt 255 feeds extruder 0 (right) and vt 254 feeds
extruder 1 (left); on a single-nozzle printer tray 0 is the only external holder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.location import Location
from backend.app.models.printer import Printer
from backend.app.models.printer_hardware import PrinterFilamentPath, PrinterNozzle

logger = logging.getLogger(__name__)

NOZZLE_TYPES: tuple[str, ...] = (
    "hardened_steel",
    "stainless_steel",
    "tungsten_carbide",
    "brass",
    "E3D",
    "undefine",
)
VOLUME_TYPES: tuple[str, ...] = (
    "Standard",
    "High Flow",
    "TPU High Flow",
    "E3D High Flow",
    "Hybrid",
    "Extra High Flow",
)
TRACK_SWITCH_MODES: tuple[str, ...] = ("auto", "installed", "absent")

DEFAULT_DIAMETER_MM = 0.4
DEFAULT_NOZZLE_TYPE = "undefine"
DEFAULT_VOLUME_TYPE = "Standard"

EXTERNAL_AMS_ID = 255

_VOLUME_BY_PREFIX = {
    "S": "Standard",
    "A": "Standard",
    "X": "Standard",
    "H": "High Flow",
    "E": "High Flow",
    "U": "TPU High Flow",
    "B": "E3D High Flow",
}
_MATERIAL_BY_CODE = {
    "00": "stainless_steel",
    "01": "hardened_steel",
    "05": "tungsten_carbide",
}


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode_bambu_nozzle_type(raw: str | None) -> tuple[str, str] | None:
    """Decode a Bambu nozzle type string into ``(nozzle_type, volume_type)``.

    Handles both the H2-series compact code from ``device.nozzle.info[].type``
    (e.g. ``"HS01"``: char0 = flow family S/A/X Standard, H/E High Flow,
    U TPU High Flow, B E3D High Flow; trailing digits 00 stainless steel,
    01 hardened steel, 05 tungsten carbide) and the
    legacy long names older firmware sends in ``nozzle_type`` (e.g.
    ``"hardened_steel"``). Returns None for empty or ``"N/A"`` values.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or value.upper() in ("N/A", "NA", "NONE"):
        return None
    if value in NOZZLE_TYPES:
        return value, DEFAULT_VOLUME_TYPE
    lowered = value.lower()
    if lowered in NOZZLE_TYPES:
        return lowered, DEFAULT_VOLUME_TYPE
    if lowered == "e3d":
        return "E3D", DEFAULT_VOLUME_TYPE
    # Compact code: first char is the flow family, the trailing two digits the
    # material ("HS01" -> High Flow, hardened steel). Short forms such as "HS"
    # carry no material code.
    if 2 <= len(value) <= 8:
        volume = _VOLUME_BY_PREFIX.get(value[0].upper())
        material = _MATERIAL_BY_CODE.get(value[-2:]) if len(value) >= 4 else None
        if volume:
            return material or DEFAULT_NOZZLE_TYPE, volume
    return None


def _parse_diameter(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 5:
        return None
    return round(value, 3)


def decode_live_nozzle(raw_type: Any, raw_diameter: Any) -> dict | None:
    """Build the ``live`` block for one nozzle, or None when nothing is known."""
    raw_type_str = str(raw_type).strip() if raw_type is not None else ""
    diameter = _parse_diameter(raw_diameter)
    decoded = decode_bambu_nozzle_type(raw_type_str)
    if decoded is None and diameter is None:
        return None
    nozzle_type, volume_type = decoded if decoded else (DEFAULT_NOZZLE_TYPE, DEFAULT_VOLUME_TYPE)
    return {
        "diameter_mm": diameter,
        "nozzle_type": nozzle_type,
        "volume_type": volume_type,
        "raw_type": raw_type_str or None,
    }


def live_nozzles_from_state(state: Any) -> dict[int, dict]:
    """Return ``{extruder_index: live_block}`` from a PrinterState."""
    result: dict[int, dict] = {}
    nozzles = getattr(state, "nozzles", None) or []
    for idx, info in enumerate(nozzles):
        live = decode_live_nozzle(getattr(info, "nozzle_type", None), getattr(info, "nozzle_diameter", None))
        if live is not None:
            result[idx] = live
    return result


def rack_from_state(state: Any) -> list[dict]:
    """Decode H2C nozzle-rack docks (ids other than the two mounted hotends)."""
    rack: list[dict] = []
    for entry in getattr(state, "nozzle_rack", None) or []:
        if not isinstance(entry, dict):
            continue
        try:
            rack_id = int(entry.get("id", -1))
        except (TypeError, ValueError):
            continue
        if rack_id in (0, 1) or rack_id < 0:
            continue
        live = decode_live_nozzle(entry.get("type"), entry.get("diameter"))
        rack.append(
            {
                "id": rack_id,
                "diameter_mm": live["diameter_mm"] if live else None,
                "nozzle_type": live["nozzle_type"] if live else None,
                "volume_type": live["volume_type"] if live else None,
                "raw_type": live["raw_type"] if live else None,
                "wear": entry.get("wear"),
                "stat": entry.get("stat"),
                "serial_number": entry.get("serial_number") or None,
            }
        )
    return rack


# ---------------------------------------------------------------------------
# Printer state helpers
# ---------------------------------------------------------------------------


def _get_state(printer_id: int) -> Any:
    from backend.app.services.printer_manager import printer_manager

    try:
        return printer_manager.get_status(printer_id)
    except Exception:  # noqa: BLE001
        return None


def _is_bambu(printer: Printer) -> bool:
    return (printer.connection_type or "bambu") == "bambu"


def effective_nozzle_count(printer: Printer, state: Any, manual_indexes: set[int] | None = None) -> int:
    count = max(1, int(printer.nozzle_count or 1))
    if state is not None:
        live = live_nozzles_from_state(state)
        if 1 in live and _is_bambu(printer):
            count = max(count, 2)
        ams_map = getattr(state, "ams_extruder_map", None) or {}
        if any(str(v) == "1" for v in ams_map.values()):
            count = max(count, 2)
    if manual_indexes:
        count = max(count, max(manual_indexes) + 1)
    return count


def position_label(printer: Printer, nozzle_count: int, extruder_index: int) -> str:
    if _is_bambu(printer) and nozzle_count == 2 and extruder_index in (0, 1):
        return "right" if extruder_index == 0 else "left"
    return f"T{extruder_index}"


# ---------------------------------------------------------------------------
# Nozzles
# ---------------------------------------------------------------------------


async def get_nozzles(db: AsyncSession, printer: Printer) -> dict:
    """Merged nozzle view: manual row, else live MQTT, else stored mqtt row, else defaults."""
    rows = (await db.execute(select(PrinterNozzle).where(PrinterNozzle.printer_id == printer.id))).scalars().all()
    by_index = {r.extruder_index: r for r in rows}
    state = _get_state(printer.id)
    live = live_nozzles_from_state(state) if state is not None else {}
    count = effective_nozzle_count(printer, state, set(by_index.keys()))
    active_extruder = getattr(state, "active_extruder", 0) if state is not None else 0

    nozzles: list[dict] = []
    for idx in range(count):
        row = by_index.get(idx)
        live_block = live.get(idx)
        if row is not None and row.source == "manual":
            diameter, ntype, vtype, source = row.diameter_mm, row.nozzle_type, row.volume_type, "manual"
        elif live_block is not None:
            diameter = live_block["diameter_mm"]
            ntype, vtype, source = live_block["nozzle_type"], live_block["volume_type"], "mqtt"
            if diameter is None and row is not None:
                diameter = row.diameter_mm
        elif row is not None:
            diameter, ntype, vtype, source = row.diameter_mm, row.nozzle_type, row.volume_type, row.source
        else:
            diameter, ntype, vtype, source = None, None, None, "default"
        nozzles.append(
            {
                "extruder_index": idx,
                "position": position_label(printer, count, idx),
                "diameter_mm": diameter if diameter is not None else DEFAULT_DIAMETER_MM,
                "nozzle_type": ntype or DEFAULT_NOZZLE_TYPE,
                "volume_type": vtype or DEFAULT_VOLUME_TYPE,
                "source": source,
                "live": live_block,
                "active": (idx == active_extruder) if count > 1 else True,
            }
        )
    return {
        "nozzle_count": count,
        "nozzles": nozzles,
        "rack": rack_from_state(state) if state is not None else [],
    }


async def set_manual_nozzle(
    db: AsyncSession,
    printer_id: int,
    extruder_index: int,
    diameter_mm: float,
    nozzle_type: str,
    volume_type: str,
) -> None:
    row = (
        await db.execute(
            select(PrinterNozzle).where(
                PrinterNozzle.printer_id == printer_id, PrinterNozzle.extruder_index == extruder_index
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PrinterNozzle(printer_id=printer_id, extruder_index=extruder_index)
        db.add(row)
    row.diameter_mm = diameter_mm
    row.nozzle_type = nozzle_type
    row.volume_type = volume_type
    row.raw_type = None
    row.source = "manual"
    await db.commit()


async def clear_manual_nozzle(db: AsyncSession, printer_id: int, extruder_index: int) -> bool:
    """Drop the manual override. Returns False when there was none."""
    row = (
        await db.execute(
            select(PrinterNozzle).where(
                PrinterNozzle.printer_id == printer_id, PrinterNozzle.extruder_index == extruder_index
            )
        )
    ).scalar_one_or_none()
    if row is None or row.source != "manual":
        return False
    state = _get_state(printer_id)
    live = live_nozzles_from_state(state).get(extruder_index) if state is not None else None
    if live is not None:
        row.source = "mqtt"
        row.diameter_mm = live["diameter_mm"]
        row.nozzle_type = live["nozzle_type"]
        row.volume_type = live["volume_type"]
        row.raw_type = live["raw_type"]
    else:
        await db.delete(row)
    await db.commit()
    _LAST_PERSISTED.pop(printer_id, None)
    return True


# printer_id -> {extruder_index: (diameter, nozzle_type, volume_type, raw_type)}
_LAST_PERSISTED: dict[int, dict[int, tuple]] = {}


async def observe_live_nozzles(printer_id: int, state: Any) -> bool:
    """Persist decoded live nozzle values as ``source='mqtt'`` when they change.

    Called from the printer status-change callback, so it keeps an in-memory
    snapshot and only touches the database when the decoded values differ from
    what it last wrote. Manual rows are never overwritten. Returns True when a
    write happened.
    """
    live = live_nozzles_from_state(state)
    if not live:
        return False
    snapshot = {
        idx: (blk["diameter_mm"], blk["nozzle_type"], blk["volume_type"], blk["raw_type"]) for idx, blk in live.items()
    }
    if _LAST_PERSISTED.get(printer_id) == snapshot:
        return False

    from backend.app.core.database import async_session

    wrote = False
    async with async_session() as db:
        if (await db.get(Printer, printer_id)) is None:
            return False
        rows = (await db.execute(select(PrinterNozzle).where(PrinterNozzle.printer_id == printer_id))).scalars().all()
        by_index = {r.extruder_index: r for r in rows}
        for idx, values in snapshot.items():
            diameter, ntype, vtype, raw = values
            row = by_index.get(idx)
            if row is not None and row.source == "manual":
                continue
            if row is None:
                row = PrinterNozzle(printer_id=printer_id, extruder_index=idx, source="mqtt")
                db.add(row)
            elif (row.diameter_mm, row.nozzle_type, row.volume_type, row.raw_type) == (
                diameter if diameter is not None else row.diameter_mm,
                ntype,
                vtype,
                raw,
            ):
                continue
            if diameter is not None:
                row.diameter_mm = diameter
            row.nozzle_type = ntype
            row.volume_type = vtype
            row.raw_type = raw
            row.source = "mqtt"
            wrote = True
        if wrote:
            await db.commit()
    _LAST_PERSISTED[printer_id] = snapshot
    return wrote


# ---------------------------------------------------------------------------
# Filament paths
# ---------------------------------------------------------------------------


@dataclass
class SlotPath:
    ams_id: int
    tray_id: int
    label: str
    ams_type: str
    extruders: list[int] = field(default_factory=list)
    source: str = "default"

    @property
    def fixed(self) -> bool:
        return len(self.extruders) == 1

    def as_dict(self) -> dict:
        return {
            "ams_id": self.ams_id,
            "tray_id": self.tray_id,
            "label": self.label,
            "ams_type": self.ams_type,
            "extruders": list(self.extruders),
            "fixed": self.fixed,
            "source": self.source,
        }


def _ams_letter(ams_id: int) -> str:
    return chr(ord("A") + ams_id) if 0 <= ams_id < 26 else str(ams_id)


def slot_label(ams_id: int, tray_id: int, nozzle_count: int = 1) -> str:
    if ams_id == EXTERNAL_AMS_ID:
        if nozzle_count >= 2:
            return "External left" if tray_id == 0 else "External right"
        return "External" if tray_id == 0 else f"External {tray_id + 1}"
    if ams_id >= 128:
        return f"HT {ams_id - 128 + 1}"
    return f"AMS {_ams_letter(ams_id)} slot {tray_id + 1}"


def unit_label(ams_id: int) -> str:
    if ams_id == EXTERNAL_AMS_ID:
        return "External"
    if ams_id >= 128:
        return f"HT {ams_id - 128 + 1}"
    return f"AMS {_ams_letter(ams_id)}"


def _ams_type(ams_id: int) -> str:
    if ams_id == EXTERNAL_AMS_ID:
        return "External"
    if ams_id >= 128:
        return "AMS HT"
    return "AMS"


def external_tray_extruder(tray_id: int, nozzle_count: int) -> int:
    """External tray -> extruder (tray 0 = vt 254 = left/1, tray 1 = vt 255 = right/0 on dual)."""
    if nozzle_count >= 2:
        return 1 if tray_id == 0 else 0
    return 0


def _ams_units_from_state(state: Any) -> list[dict]:
    raw = getattr(state, "raw_data", None) or {}
    ams = raw.get("ams", [])
    if isinstance(ams, dict):
        ams = ams.get("ams", [])
    return [u for u in ams if isinstance(u, dict)] if isinstance(ams, list) else []


def live_slots_from_state(state: Any) -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for unit in _ams_units_from_state(state):
        try:
            ams_id = int(unit.get("id", 0))
        except (TypeError, ValueError):
            continue
        trays = unit.get("tray") or []
        tray_ids: list[int] = []
        for i, tray in enumerate(trays):
            try:
                tray_ids.append(int(tray.get("id", i)) if isinstance(tray, dict) else i)
            except (TypeError, ValueError):
                tray_ids.append(i)
        if not tray_ids:
            tray_ids = [0] if ams_id >= 128 else [0, 1, 2, 3]
        for tid in tray_ids:
            slots.append((ams_id, tid))
    return slots


def _fila_switch(state: Any) -> Any:
    return getattr(state, "fila_switch", None) if state is not None else None


def _inlet_ams(value: Any) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 0 or v >= 254:
        return None
    if v >= 128:
        return v
    return v // 4


def track_switch_info(printer: Printer, state: Any) -> dict:
    mode = (printer.track_switch_mode or "auto").lower()
    fs = _fila_switch(state)
    live_installed = bool(getattr(fs, "installed", False)) if fs is not None else False
    if mode == "installed":
        installed, source = True, "manual"
    elif mode == "absent":
        installed, source = False, "manual"
    else:
        installed, source = live_installed, "mqtt"
    in_slots = list(getattr(fs, "in_slots", []) or []) if fs is not None else []
    inlets = {
        "A": _inlet_ams(in_slots[0]) if len(in_slots) > 0 else None,
        "B": _inlet_ams(in_slots[1]) if len(in_slots) > 1 else None,
    }
    return {"installed": installed, "source": source, "mode": mode, "inlets": inlets}


async def _manual_paths(db: AsyncSession, printer_id: int) -> list[PrinterFilamentPath]:
    return list(
        (await db.execute(select(PrinterFilamentPath).where(PrinterFilamentPath.printer_id == printer_id)))
        .scalars()
        .all()
    )


def _clean_extruders(values: Any) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in out:
            out.append(iv)
    return sorted(out)


async def compute_filament_paths(db: AsyncSession, printer: Printer) -> dict:
    """Return ``{"track_switch": {...}, "slots": [...], "nozzle_count": n}``."""
    state = _get_state(printer.id)
    manual = await _manual_paths(db, printer.id)
    nozzle_rows = (
        (await db.execute(select(PrinterNozzle.extruder_index).where(PrinterNozzle.printer_id == printer.id)))
        .scalars()
        .all()
    )
    count = effective_nozzle_count(printer, state, {i for i in nozzle_rows if i is not None})
    switch = track_switch_info(printer, state)
    ams_map = (getattr(state, "ams_extruder_map", None) or {}) if state is not None else {}

    slot_keys: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()

    def _add(key: tuple[int, int]) -> None:
        if key not in seen:
            seen.add(key)
            slot_keys.append(key)

    if state is not None:
        for key in live_slots_from_state(state):
            _add(key)
    for row in manual:
        if row.tray_id is not None:
            _add((row.ams_id, row.tray_id))
        elif row.ams_id != EXTERNAL_AMS_ID:
            for tid in [0] if row.ams_id >= 128 else [0, 1, 2, 3]:
                _add((row.ams_id, tid))
    for tid in range(2 if count >= 2 else 1):
        _add((EXTERNAL_AMS_ID, tid))

    manual_slot = {(r.ams_id, r.tray_id): r for r in manual if r.tray_id is not None}
    manual_unit = {r.ams_id: r for r in manual if r.tray_id is None}
    all_extruders = list(range(count))

    slots: list[SlotPath] = []
    for ams_id, tray_id in sorted(slot_keys, key=lambda k: (k[0] == EXTERNAL_AMS_ID, k[0], k[1])):
        sp = SlotPath(
            ams_id=ams_id,
            tray_id=tray_id,
            label=slot_label(ams_id, tray_id, count),
            ams_type=_ams_type(ams_id),
        )
        row = manual_slot.get((ams_id, tray_id)) or manual_unit.get(ams_id)
        if row is not None and _clean_extruders(row.extruders):
            sp.extruders = _clean_extruders(row.extruders)
            sp.source = "manual"
        elif ams_id == EXTERNAL_AMS_ID:
            sp.extruders = [external_tray_extruder(tray_id, count)]
            sp.source = "mqtt" if (count >= 2 and ams_map) else "default"
        elif switch["installed"] and count >= 2:
            sp.extruders = [0, 1]
            sp.source = switch["source"]
        elif str(ams_id) in ams_map:
            try:
                sp.extruders = [int(ams_map[str(ams_id)])]
                sp.source = "mqtt"
            except (TypeError, ValueError):
                sp.extruders = all_extruders
        else:
            sp.extruders = [0] if count == 1 else all_extruders
            sp.source = "default"
        slots.append(sp)

    return {
        "nozzle_count": count,
        "track_switch": {
            "installed": switch["installed"],
            "source": switch["source"],
            "mode": switch["mode"],
            "inlets": switch["inlets"],
        },
        "slots": [s.as_dict() for s in slots],
    }


async def allowed_extruders_for_slot(db: AsyncSession, printer: Printer, ams_id: int, tray_id: int) -> list[int]:
    paths = await compute_filament_paths(db, printer)
    for slot in paths["slots"]:
        if slot["ams_id"] == ams_id and slot["tray_id"] == tray_id:
            return list(slot["extruders"])
    # Unknown slot (offline printer, no manual row): fall back to defaults.
    count = paths["nozzle_count"]
    if ams_id == EXTERNAL_AMS_ID:
        return [external_tray_extruder(tray_id, count)]
    return list(range(count))


class ExtruderNotAllowed(Exception):
    def __init__(self, extruder_index: int, allowed: list[int]):
        super().__init__(f"Extruder {extruder_index} cannot be fed from this slot")
        self.extruder_index = extruder_index
        self.allowed = allowed


async def resolve_assignment_extruder(
    db: AsyncSession, printer: Printer, ams_id: int, tray_id: int, requested: int | None
) -> int | None:
    """Validate/infer the extruder for a slot assignment.

    Raises ExtruderNotAllowed when ``requested`` is not reachable from the slot.
    When nothing is requested and the slot feeds exactly one extruder, that one
    is returned; otherwise None.
    """
    allowed = await allowed_extruders_for_slot(db, printer, ams_id, tray_id)
    if requested is not None:
        if requested not in allowed:
            raise ExtruderNotAllowed(requested, allowed)
        return requested
    if len(allowed) == 1:
        return allowed[0]
    return None


async def replace_manual_paths(
    db: AsyncSession, printer: Printer, track_switch_mode: str | None, slots: list[dict] | None
) -> None:
    if track_switch_mode is not None:
        printer.track_switch_mode = track_switch_mode
    if slots is not None:
        for row in await _manual_paths(db, printer.id):
            await db.delete(row)
        await db.flush()
        for slot in slots:
            db.add(
                PrinterFilamentPath(
                    printer_id=printer.id,
                    ams_id=int(slot["ams_id"]),
                    tray_id=slot.get("tray_id"),
                    extruders=_clean_extruders(slot.get("extruders")),
                    source="manual",
                )
            )
    await db.commit()


# ---------------------------------------------------------------------------
# Slot locations
# ---------------------------------------------------------------------------


async def find_slot_location(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> Location | None:
    """The ``ams_slot``/``external`` location row bound to a slot, if one exists."""
    result = await db.execute(
        select(Location)
        .where(
            Location.printer_id == printer_id,
            Location.ams_id == ams_id,
            Location.tray_id == tray_id,
            Location.kind.in_(("ams_slot", "external")),
        )
        .order_by(Location.id)
    )
    return result.scalars().first()
