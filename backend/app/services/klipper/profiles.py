"""Klipper printer profile registry.

Adding support for another Klipper printer is a *data* change: add one
``KlipperProfile`` entry to ``KLIPPER_PROFILES`` below. No new code paths are
required — the connection layer (Moonraker) and the data layer (capabilities)
read everything they need from the profile.

Covers the Voron family (V0, Trident, 2.4, Switchwire). ``voron_2.4_350`` is the
profile verified against real hardware; the others share the same code paths
and differ only in geometry + the bed-levelling macro, but are unverified until
someone tests them (``experimental=True``).

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


# Macro presets for the levelling styles.
_QGL = KlipperMacros()  # QUAD_GANTRY_LEVEL (2.4)
_ZTILT = KlipperMacros(level="Z_TILT_ADJUST")  # Trident
_NO_LEVEL = KlipperMacros(level=None)  # single-Z (V0, Switchwire)


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
