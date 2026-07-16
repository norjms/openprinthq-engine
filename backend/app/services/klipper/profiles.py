"""Klipper printer profile registry.

Adding support for another Klipper printer is a *data* change: add one
``KlipperProfile`` entry to ``KLIPPER_PROFILES`` below. No new code paths are
required — the connection layer (Moonraker) and the data layer (capabilities)
read everything they need from the profile.

Covers the Voron family plus popular Klipper-based consumer printers (Creality
K-series, Qidi, Sovol, RatRig, Vzbot, FlashForge AD5M, Elegoo Centauri, FLSun,
Anycubic, BIQU) — all controllable through the existing Moonraker client.
``voron_2.4_350`` is the profile verified against real hardware; the others
share the same code paths and differ only in geometry + the bed-levelling
macro, but are unverified until someone tests them (``experimental=True``).

The profile key is stored in ``Printer.model`` for Klipper rows (Bambu rows
keep their Bambu model string there).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KlipperMacros:
    """Names of the gcode macros / commands used for printer control.

    Defaults are stock Klipper/Voron. Override per-profile if a printer's
    config uses different macro names.
    """

    home: str = "G28"
    # Bed/gantry levelling command. QUAD_GANTRY_LEVEL on the 2.4, Z_TILT_ADJUST
    # on the Trident, and None on single-Z machines (V0, Switchwire) which have
    # no auto-levelling — the UI hides the button when this is None.
    level: str | None = "QUAD_GANTRY_LEVEL"
    emergency_stop: str = "M112"
    # Chamber light: stock Voron exposes an [output_pin caselight]; the SET_PIN
    # form is the most portable. Configs using an [led] strip differ — this is
    # config-dependent and may need per-install adjustment.
    light_on: str | None = "SET_PIN PIN=caselight VALUE=1"
    light_off: str | None = "SET_PIN PIN=caselight VALUE=0"


@dataclass(frozen=True)
class KlipperProfile:
    """Capability + geometry description for one Klipper printer model."""

    key: str  # stable identifier, stored in Printer.model
    label: str  # human-friendly name shown in the UI
    bed_size_mm: tuple[int, int]  # (x, y)
    extruder_count: int = 1
    # has_chamber may be True even when a given install lacks a chamber sensor:
    # the client auto-discovers the actual chamber object at connect time and
    # only surfaces a chamber reading when one exists, so True is always safe.
    has_chamber: bool = True
    # Fallback chamber object name; the client overrides this via auto-discovery
    # from /printer/objects/list, so it rarely matters.
    chamber_object: str | None = "temperature_sensor chamber"
    macros: KlipperMacros = field(default_factory=KlipperMacros)
    # Human label for the levelling action (shown on the printer card menu).
    # None when the printer has no auto-levelling.
    leveling_label: str | None = "Quad Gantry Level"
    experimental: bool = True  # False only once verified against real hardware


# Macro presets for the levelling styles. Klipper printers expose different
# bed/gantry-levelling commands depending on kinematics + Z-motor count.
_QGL = KlipperMacros()  # QUAD_GANTRY_LEVEL — 4-Z gantry (Voron 2.4, SV08)
_ZTILT = KlipperMacros(level="Z_TILT_ADJUST")  # 3-Z bed (Trident, RatRig V-Core)
_BEDMESH = KlipperMacros(level="BED_MESH_CALIBRATE")  # single-Z CoreXY / bed-slingers
_DELTA = KlipperMacros(level="DELTA_CALIBRATE")  # delta kinematics (FLSun)
_NO_LEVEL = KlipperMacros(level=None)  # single-Z, no auto-level (V0, Switchwire)

# Leveling labels paired with the presets above.
_L_QGL = "Quad Gantry Level"
_L_ZTILT = "Z Tilt Adjust"
_L_MESH = "Bed Mesh Calibrate"
_L_DELTA = "Delta Calibrate"


# ---------------------------------------------------------------------------
# Registry — add new printers here.
# ---------------------------------------------------------------------------
KLIPPER_PROFILES: dict[str, KlipperProfile] = {
    # --- Voron 2.4 (CoreXY, quad gantry) ---------------------------------
    "voron_2.4_250": KlipperProfile(
        key="voron_2.4_250", label="Voron 2.4 (250mm)", bed_size_mm=(250, 250)
    ),
    "voron_2.4_300": KlipperProfile(
        key="voron_2.4_300", label="Voron 2.4 (300mm)", bed_size_mm=(300, 300)
    ),
    "voron_2.4_350": KlipperProfile(
        key="voron_2.4_350",
        label="Voron 2.4 (350mm)",
        bed_size_mm=(350, 350),
        experimental=False,  # the profile verified against real hardware
    ),
    # --- Voron Trident (CoreXY, Z-tilt) ----------------------------------
    "voron_trident_250": KlipperProfile(
        key="voron_trident_250", label="Voron Trident (250mm)", bed_size_mm=(250, 250),
        macros=_ZTILT, leveling_label="Z Tilt Adjust",
    ),
    "voron_trident_300": KlipperProfile(
        key="voron_trident_300", label="Voron Trident (300mm)", bed_size_mm=(300, 300),
        macros=_ZTILT, leveling_label="Z Tilt Adjust",
    ),
    "voron_trident_350": KlipperProfile(
        key="voron_trident_350", label="Voron Trident (350mm)", bed_size_mm=(350, 350),
        macros=_ZTILT, leveling_label="Z Tilt Adjust",
    ),
    # --- Voron V0 (CoreXZ, single Z, no auto-level) ----------------------
    "voron_v0": KlipperProfile(
        key="voron_v0", label="Voron V0", bed_size_mm=(120, 120),
        macros=_NO_LEVEL, leveling_label=None,
    ),
    # --- Voron Switchwire (belt-driven Z, single Z, no auto-level) -------
    "voron_switchwire": KlipperProfile(
        key="voron_switchwire", label="Voron Switchwire", bed_size_mm=(250, 210),
        macros=_NO_LEVEL, leveling_label=None,
    ),
    # ===================================================================
    # Curated popular Klipper printers (Chunk 0). All controllable via the
    # existing Moonraker client. Geometry from OrcaSlicer profiles; levelling
    # from kinematics. experimental=True until verified on hardware.
    # ===================================================================
    # --- Creality (CoreXY K-series + bed-slinger Ender Klipper) -----------
    "creality_k1": KlipperProfile(key="creality_k1", label="Creality K1", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    "creality_k1c": KlipperProfile(key="creality_k1c", label="Creality K1C", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    "creality_k1_max": KlipperProfile(key="creality_k1_max", label="Creality K1 Max", bed_size_mm=(300, 300), macros=_BEDMESH, leveling_label=_L_MESH),
    "creality_k2_plus": KlipperProfile(key="creality_k2_plus", label="Creality K2 Plus", bed_size_mm=(350, 350), macros=_BEDMESH, leveling_label=_L_MESH),
    "creality_ender3_v3_ke": KlipperProfile(key="creality_ender3_v3_ke", label="Creality Ender-3 V3 KE", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    "creality_ender3_v3": KlipperProfile(key="creality_ender3_v3", label="Creality Ender-3 V3", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- Qidi (CoreXY, heated chamber) -----------------------------------
    "qidi_plus4": KlipperProfile(key="qidi_plus4", label="Qidi Plus4", bed_size_mm=(305, 305), macros=_BEDMESH, leveling_label=_L_MESH),
    "qidi_xmax3": KlipperProfile(key="qidi_xmax3", label="Qidi X-Max 3", bed_size_mm=(325, 325), macros=_BEDMESH, leveling_label=_L_MESH),
    "qidi_xplus3": KlipperProfile(key="qidi_xplus3", label="Qidi X-Plus 3", bed_size_mm=(280, 280), macros=_BEDMESH, leveling_label=_L_MESH),
    "qidi_q1_pro": KlipperProfile(key="qidi_q1_pro", label="Qidi Q1 Pro", bed_size_mm=(245, 245), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- Sovol -----------------------------------------------------------
    "sovol_sv08": KlipperProfile(key="sovol_sv08", label="Sovol SV08", bed_size_mm=(350, 350), macros=_QGL, leveling_label=_L_QGL),
    "sovol_sv07": KlipperProfile(key="sovol_sv07", label="Sovol SV07", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    "sovol_sv07_plus": KlipperProfile(key="sovol_sv07_plus", label="Sovol SV07 Plus", bed_size_mm=(300, 300), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- RatRig V-Core (CoreXY, 3-Z tilt) --------------------------------
    "ratrig_vcore3_300": KlipperProfile(key="ratrig_vcore3_300", label="RatRig V-Core 3 (300mm)", bed_size_mm=(300, 300), macros=_ZTILT, leveling_label=_L_ZTILT),
    "ratrig_vcore3_400": KlipperProfile(key="ratrig_vcore3_400", label="RatRig V-Core 3 (400mm)", bed_size_mm=(400, 400), macros=_ZTILT, leveling_label=_L_ZTILT),
    "ratrig_vcore3_500": KlipperProfile(key="ratrig_vcore3_500", label="RatRig V-Core 3 (500mm)", bed_size_mm=(500, 500), macros=_ZTILT, leveling_label=_L_ZTILT),
    "ratrig_vminion": KlipperProfile(key="ratrig_vminion", label="RatRig V-Minion", bed_size_mm=(180, 180), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- Vzbot (CoreXY speed) --------------------------------------------
    "vzbot_235": KlipperProfile(key="vzbot_235", label="Vzbot 235", bed_size_mm=(235, 235), macros=_BEDMESH, leveling_label=_L_MESH),
    "vzbot_330": KlipperProfile(key="vzbot_330", label="Vzbot 330", bed_size_mm=(330, 330), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- FlashForge (AD5M = CoreXY Klipper) ------------------------------
    "flashforge_ad5m": KlipperProfile(key="flashforge_ad5m", label="FlashForge Adventurer 5M", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    "flashforge_ad5m_pro": KlipperProfile(key="flashforge_ad5m_pro", label="FlashForge Adventurer 5M Pro", bed_size_mm=(220, 220), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- Elegoo Centauri (CoreXY Klipper) --------------------------------
    "elegoo_centauri": KlipperProfile(key="elegoo_centauri", label="Elegoo Centauri", bed_size_mm=(256, 256), macros=_BEDMESH, leveling_label=_L_MESH),
    "elegoo_centauri_carbon": KlipperProfile(key="elegoo_centauri_carbon", label="Elegoo Centauri Carbon", bed_size_mm=(256, 256), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- FLSun (delta) ---------------------------------------------------
    "flsun_v400": KlipperProfile(key="flsun_v400", label="FLSun V400", bed_size_mm=(300, 300), macros=_DELTA, leveling_label=_L_DELTA),
    "flsun_s1": KlipperProfile(key="flsun_s1", label="FLSun S1", bed_size_mm=(320, 320), macros=_DELTA, leveling_label=_L_DELTA),
    # --- Anycubic (Klipper models) ---------------------------------------
    "anycubic_kobra_s1": KlipperProfile(key="anycubic_kobra_s1", label="Anycubic Kobra S1", bed_size_mm=(250, 250), macros=_BEDMESH, leveling_label=_L_MESH),
    "anycubic_kobra_3": KlipperProfile(key="anycubic_kobra_3", label="Anycubic Kobra 3", bed_size_mm=(250, 250), macros=_BEDMESH, leveling_label=_L_MESH),
    # --- BIQU ------------------------------------------------------------
    "biqu_hurakan": KlipperProfile(key="biqu_hurakan", label="BIQU Hurakan", bed_size_mm=(235, 235), macros=_BEDMESH, leveling_label=_L_MESH),
}

# Default when a stored profile key is unknown/missing. The 350 2.4 is the
# verified profile and the safest generic fallback.
DEFAULT_KLIPPER_PROFILE_KEY = "voron_2.4_350"


def get_profile(key: str | None) -> KlipperProfile:
    """Resolve a profile key to a ``KlipperProfile``.

    Falls back to the default profile for unknown/missing keys so an
    unrecognised stored value never crashes the connection layer.
    """
    if key and key in KLIPPER_PROFILES:
        return KLIPPER_PROFILES[key]
    return KLIPPER_PROFILES[DEFAULT_KLIPPER_PROFILE_KEY]


def list_profiles() -> list[KlipperProfile]:
    """All registered profiles, for the add-printer UI dropdown."""
    return list(KLIPPER_PROFILES.values())
