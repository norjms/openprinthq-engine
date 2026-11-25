"""Creality CFS and QIDI Box slot parsing for Moonraker printers.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware: built from published
protocol research, see docs/multi-material-units.md for the sources.

Both units live inside Klipper and reach us through ordinary Moonraker object
subscriptions, so the existing ``MoonrakerClient`` only has to subscribe to a
few more objects and hand them to ``MaterialObjectTracker``.

Creality CFS (K1 with the CFS upgrade, K2 / K2 Plus / K2 Pro)
    One ``box`` object. The stock schema has ``T1``..``T4`` (one per chained
    unit) each holding four parallel per-slot arrays: ``material_type`` (a
    Creality material code such as ``101001``), ``color_value`` (``0RRGGBB``),
    ``vender`` (``"none"`` = empty bay, ``"unknown"`` = spool without a tag,
    otherwise the RFID record) and ``remain_len`` (metres). Frames are deltas,
    so the tracker merges them. Community Kalico ports publish a flat schema
    (``slots[]`` with ``material``/``color``/``present``), handled as well.
    Tool remap: ``BOX_MODIFY_TN T<tool>=T<slot>`` (slot names ``T1A``..``T4D``),
    which the firmware keeps until changed.

QIDI Box (Plus4, Q2, Max4)
    ``save_variables`` holds the box state: ``box_count``, ``slot<N>`` (0 empty,
    1/3 present, 2 loaded, negative blocked), ``filament_slot<N>`` /
    ``color_slot<N>`` / ``vendor_slot<N>`` (row ids into the printer's
    ``officiall_filas_list.cfg``; newer box firmware may write a product id
    such as ``'QFA90'`` and a colour list ``['228332']`` instead), and the tool
    map ``value_t<N> = "slot<M>"``. ``multi_color_controller`` (Max 4 and newer
    Q2 firmware) states each slot's resolved material. Per-box humidity and
    temperature come from ``aht20_f heater_box<N>``.
    Tool remap: ``SAVE_VARIABLE VARIABLE=value_t<N> VALUE='"slot<M>"'``.
"""

from __future__ import annotations

import configparser
import re

from backend.app.services.material_slots import (
    SLOTS_PER_UNIT,
    UNIT_CFS,
    UNIT_QIDI_BOX,
    MaterialSlot,
    MaterialUnit,
    normalize_hex,
)

# Creality material codes seen in the stock ``box`` object. Generic codes
# appear both as 1000NN and as the 0-prefixed 0000NN form K1 firmware stores.
# Codes not listed fall back to the ``same_material`` groups, which carry the
# material name the printer resolved.
_CFS_CODE_MATERIAL = {
    "101001": "PLA",  # Creality Hyper PLA
    "102001": "PLA-CF",  # Creality Hyper PLA-CF
    "103001": "ABS",  # Creality Hyper ABS
    "106002": "PETG",  # Creality Hyper PETG
}
_CFS_GENERIC_SUFFIX = {
    "001": "PLA",
    "002": "PLA-CF",
    "003": "PETG",
    "004": "ABS",
    "005": "TPU",
}
_CFS_EMPTY = {"", "-1", "none", "None", "unknown"}

# Klipper config section names that tell us a unit is present.
CFS_OBJECT = "box"
QIDI_DETECT_PREFIX = "box_stepper slot"
QIDI_OBJECTS = ("save_variables", "multi_color_controller")
QIDI_ENV_PREFIX = "aht20_f heater_box"


def cfs_slot_name(global_slot: int) -> str:
    """Global slot 0..15 -> ``T1A``..``T4D``."""
    return f"T{global_slot // SLOTS_PER_UNIT + 1}{'ABCD'[global_slot % SLOTS_PER_UNIT]}"


def deep_merge(dst: dict, src: dict) -> dict:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            deep_merge(dst[key], value)
        else:
            dst[key] = value
    return dst


def _cfs_material(code, same_material: list) -> str | None:
    text = str(code or "").strip()
    if text in _CFS_EMPTY:
        return None
    for group in same_material or []:
        if isinstance(group, list) and len(group) >= 4 and str(group[0]) == text and group[3]:
            return str(group[3]).upper()
    if text in _CFS_CODE_MATERIAL:
        return _CFS_CODE_MATERIAL[text]
    if len(text) == 6 and text[1:3] == "00":
        return _CFS_GENERIC_SUFFIX.get(text[3:])
    return None


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def parse_cfs_box(box: dict) -> list[MaterialUnit]:
    """Parse a merged ``box`` object (stock or flat schema) into units."""
    if not isinstance(box, dict):
        return []
    if any(re.fullmatch(r"T[1-4]", k) for k in box):
        return _parse_cfs_stock(box)
    if isinstance(box.get("slots"), list):
        return _parse_cfs_flat(box)
    return []


def _parse_cfs_stock(box: dict) -> list[MaterialUnit]:
    units: list[MaterialUnit] = []
    same = box.get("same_material") or []
    for n in range(1, 5):
        raw = box.get(f"T{n}")
        if not isinstance(raw, dict) or str(raw.get("state")) != "connect":
            continue
        unit = MaterialUnit(
            kind=UNIT_CFS,
            unit_id=n - 1,
            humidity=_num(raw.get("dry_and_humidity")),
            temperature=_num(raw.get("temperature")),
            serial=str(raw.get("sn")) if raw.get("sn") not in (None, "-1", "None") else None,
            firmware=str(raw.get("version")) if raw.get("version") not in (None, "-1", "None") else None,
        )
        vendors = raw.get("vender") or []
        codes = raw.get("material_type") or []
        colours = raw.get("color_value") or []
        for i in range(SLOTS_PER_UNIT):
            vend = str(vendors[i]) if i < len(vendors) else "none"
            code = codes[i] if i < len(codes) else None
            colour = colours[i] if i < len(colours) else None
            present = vend.lower() != "none" and not (str(code) in _CFS_EMPTY and str(colour) in _CFS_EMPTY)
            unit.slots.append(
                MaterialSlot(
                    index=i,
                    present=present,
                    material=_cfs_material(code, same) if present else None,
                    colour=normalize_hex(colour) if present else None,
                )
            )
        units.append(unit)
    # The stock box names the loaded bay only through ``filament``-style
    # fields that differ by firmware; leave ``active`` unset rather than guess.
    return units


def _parse_cfs_flat(box: dict) -> list[MaterialUnit]:
    units: dict[int, MaterialUnit] = {}
    loaded = box.get("loaded_slot")
    for raw in box.get("slots") or []:
        if not isinstance(raw, dict) or raw.get("external"):
            continue
        try:
            index = int(raw.get("index"))
        except (TypeError, ValueError):
            continue
        unit_id, local = divmod(index, SLOTS_PER_UNIT)
        unit = units.setdefault(unit_id, MaterialUnit(kind=UNIT_CFS, unit_id=unit_id))
        brand = raw.get("brand")
        unit.slots.append(
            MaterialSlot(
                index=local,
                present=bool(raw.get("present")),
                material=(str(raw.get("material")).upper() or None) if raw.get("material") else None,
                colour=normalize_hex(raw.get("color")),
                name=raw.get("name") or None,
                brand=None if brand in (None, "None") else str(brand),
                active=bool(raw.get("loaded")) or (loaded is not None and loaded == index),
            )
        )
    env_t, env_h = _num(box.get("temp_c")), _num(box.get("humidity_pct"))
    for unit in units.values():
        unit.temperature, unit.humidity = env_t, env_h
    return [units[k] for k in sorted(units)]


# --------------------------------------------------------------------------
# QIDI Box
# --------------------------------------------------------------------------
class QidiTables:
    """``officiall_filas_list.cfg``: filament rows, colour palette, vendors."""

    def __init__(self, text: str | None = None):
        self.filaments: dict[int, dict] = {}
        self.colours: dict[int, str] = {}
        self.vendors: dict[int, str] = {}
        if text:
            self.load(text)

    def load(self, text: str) -> None:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str  # keep numeric keys and case
        try:
            parser.read_string(text)
        except configparser.Error:
            return
        for section in parser.sections():
            m = re.fullmatch(r"fila(\d+)", section)
            if m:
                row = dict(parser.items(section))
                self.filaments[int(m.group(1))] = row
            elif section == "colordict":
                for key, value in parser.items(section):
                    if key.isdigit() and normalize_hex(value):
                        self.colours[int(key)] = normalize_hex(value)
            elif section == "vendor_list":
                for key, value in parser.items(section):
                    if key.isdigit():
                        self.vendors[int(key)] = value.strip()


def _qidi_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().strip("'\"")
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def _qidi_product_id(value) -> str | None:
    text = str(value).strip().strip("'\"") if isinstance(value, str) else ""
    return text if re.fullmatch(r"[A-Z]{2,4}\d{2,3}", text) else None


def _qidi_slot_name(value) -> int | None:
    m = re.fullmatch(r"slot(\d+)", str(value or "").strip().strip("'\""))
    return int(m.group(1)) if m else None


def parse_qidi(
    variables: dict,
    controller: dict | None = None,
    environment: dict[int, dict] | None = None,
    tables: QidiTables | None = None,
) -> list[MaterialUnit]:
    """Parse QIDI Box state. ``environment`` maps 0-based box -> aht20 object."""
    if not isinstance(variables, dict):
        return []
    tables = tables or QidiTables()
    box_count = _qidi_int(variables.get("box_count"))
    if not box_count or box_count < 0:
        return []
    box_count = min(box_count, 4)

    info: dict[int, dict] = {}
    ctrl_box = (controller or {}).get("box") if isinstance(controller, dict) else None
    if isinstance(ctrl_box, dict):
        for key, value in ctrl_box.items():
            m = re.fullmatch(r"box(\d+)_filament_info_(\d+)_(\w+)", key)
            if m and int(m.group(2)) < SLOTS_PER_UNIT:
                gslot = int(m.group(1)) * SLOTS_PER_UNIT + int(m.group(2))
                info.setdefault(gslot, {})[m.group(3)] = value

    loaded_slot = _qidi_slot_name(variables.get("last_load_slot"))
    units: list[MaterialUnit] = []
    for b in range(box_count):
        env = (environment or {}).get(b) or {}
        unit = MaterialUnit(
            kind=UNIT_QIDI_BOX,
            unit_id=b,
            humidity=_num(env.get("humidity")),
            temperature=_num(env.get("temperature")),
        )
        for local in range(SLOTS_PER_UNIT):
            g = b * SLOTS_PER_UNIT + local
            state = _qidi_int(variables.get(f"slot{g}"))
            present = state is not None and state > 0
            material = name = brand = colour = None
            tmin = tmax = None
            fil = variables.get(f"filament_slot{g}")
            row_id = _qidi_int(fil)
            if row_id and row_id > 0 and row_id in tables.filaments:
                row = tables.filaments[row_id]
                material = (row.get("type") or "").strip().upper() or None
                name = (row.get("filament") or "").strip() or None
                tmin, tmax = _qidi_int(row.get("min_temp")), _qidi_int(row.get("max_temp"))
            elif _qidi_product_id(fil) or g in info:
                ci = info.get(g, {})
                material = (str(ci.get("filament_type") or "").strip().upper()) or None
                brand = str(ci.get("vendor_str") or "").strip() or None
                tmin, tmax = _qidi_int(ci.get("min_temp")), _qidi_int(ci.get("max_temp"))
            col = variables.get(f"color_slot{g}")
            if isinstance(col, list) and col:
                colour = normalize_hex(col[0])
            else:
                cid = _qidi_int(col)
                if cid and cid > 0:
                    colour = tables.colours.get(cid)
            vid = _qidi_int(variables.get(f"vendor_slot{g}"))
            if brand is None and vid is not None and vid > 0:
                brand = tables.vendors.get(vid)
            unit.slots.append(
                MaterialSlot(
                    index=local,
                    present=present,
                    material=material if present else None,
                    colour=colour if present else None,
                    name=name if present else None,
                    brand=brand if present else None,
                    active=present and (state == 2 or loaded_slot == g),
                    nozzle_temp_min=tmin if present else None,
                    nozzle_temp_max=tmax if present else None,
                )
            )
        units.append(unit)
    return units


# --------------------------------------------------------------------------
# Remap G-code
# --------------------------------------------------------------------------
def cfs_remap_gcode(mapping: list[int]) -> str:
    """``BOX_MODIFY_TN`` lines for a remap (position = G-code tool).

    Every tool the job uses is written, remapped or not, because the firmware
    keeps the table between prints: writing the identity for an unmapped tool
    clears a remap an earlier job left behind.
    """
    lines = []
    for tool, slot in enumerate(mapping):
        if tool >= 16:
            break
        target = slot if slot is not None and slot >= 0 else tool
        lines.append(f"BOX_MODIFY_TN {cfs_slot_name(tool)}={cfs_slot_name(target)}")
    return "\n".join(lines)


def qidi_remap_gcode(mapping: list[int]) -> str:
    """``SAVE_VARIABLE value_t<N>`` lines for a remap (position = G-code tool)."""
    lines = []
    for tool, slot in enumerate(mapping):
        if tool >= 16:
            break
        target = slot if slot is not None and slot >= 0 else tool
        lines.append(f"SAVE_VARIABLE VARIABLE=value_t{tool} VALUE='\"slot{target}\"'")
    return "\n".join(lines)


class MaterialObjectTracker:
    """Keeps merged copies of the unit objects and rebuilds units on change."""

    def __init__(self):
        self.kind: str | None = None  # UNIT_CFS / UNIT_QIDI_BOX / None
        self.box: dict = {}
        self.variables: dict = {}
        self.controller: dict = {}
        self.environment: dict[int, dict] = {}
        self.env_objects: list[str] = []
        self.qidi_tables = QidiTables()

    def detect(self, object_names: list[str]) -> None:
        names = set(object_names or [])
        if CFS_OBJECT in names:
            self.kind = UNIT_CFS
        elif any(n.startswith(QIDI_DETECT_PREFIX) for n in names):
            self.kind = UNIT_QIDI_BOX
            self.env_objects = sorted(n for n in names if n.startswith(QIDI_ENV_PREFIX))
        else:
            self.kind = None
        self._has_controller = "multi_color_controller" in names

    def subscribe_objects(self) -> dict:
        if self.kind == UNIT_CFS:
            return {CFS_OBJECT: None}
        if self.kind == UNIT_QIDI_BOX:
            objs: dict = {"save_variables": None}
            if getattr(self, "_has_controller", False):
                objs["multi_color_controller"] = None
            for name in self.env_objects:
                objs[name] = ["temperature", "humidity"]
            return objs
        return {}

    def update(self, objects: dict) -> bool:
        """Merge a status frame. True when a unit object changed."""
        changed = False
        if self.kind == UNIT_CFS and isinstance(objects.get(CFS_OBJECT), dict):
            deep_merge(self.box, objects[CFS_OBJECT])
            changed = True
        elif self.kind == UNIT_QIDI_BOX:
            sv = objects.get("save_variables")
            if isinstance(sv, dict) and isinstance(sv.get("variables"), dict):
                self.variables = dict(sv["variables"])
                changed = True
            ctrl = objects.get("multi_color_controller")
            if isinstance(ctrl, dict):
                deep_merge(self.controller, ctrl)
                changed = True
            for name in self.env_objects:
                env = objects.get(name)
                if isinstance(env, dict):
                    m = re.search(r"(\d+)$", name)
                    if m:
                        self.environment.setdefault(int(m.group(1)) - 1, {}).update(env)
                        changed = True
        return changed

    def units(self) -> list[MaterialUnit]:
        if self.kind == UNIT_CFS:
            return parse_cfs_box(self.box)
        if self.kind == UNIT_QIDI_BOX:
            return parse_qidi(self.variables, self.controller, self.environment, self.qidi_tables)
        return []

    def remap_gcode(self, mapping: list[int]) -> str:
        if self.kind == UNIT_CFS:
            return cfs_remap_gcode(mapping)
        if self.kind == UNIT_QIDI_BOX:
            return qidi_remap_gcode(mapping)
        return ""
