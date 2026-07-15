"""Klipper printer profile registry.

Adding support for another Klipper printer is a *data* change: add one
``KlipperProfile`` entry to ``KLIPPER_PROFILES`` below. No new code paths are
required — the connection layer (Moonraker) and the data layer (capabilities)
read everything they need from the profile.

We currently seed and test exactly one profile, ``voron_2.4_350``. Other
printers are structurally possible but unverified; treat them as experimental
until someone tests against real hardware.

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
    level: str | None = "QUAD_GANTRY_LEVEL"  # QGL on Voron 2.4; bed mesh / Z-tilt elsewhere
    emergency_stop: str = "M112"
    # Chamber light: stock Voron exposes an [output_pin caselight]; the SET_PIN
    # form is the most portable. Profiles without a controllable light set this
    # to None and the UI hides the light button.
    light_on: str | None = "SET_PIN PIN=caselight VALUE=1"
    light_off: str | None = "SET_PIN PIN=caselight VALUE=0"


@dataclass(frozen=True)
class KlipperProfile:
    """Capability + geometry description for one Klipper printer model."""

    key: str  # stable identifier, stored in Printer.model
    label: str  # human-friendly name shown in the UI
    bed_size_mm: tuple[int, int]  # (x, y)
    extruder_count: int = 1
    has_chamber: bool = False  # heated/monitored chamber present
    # Name of the chamber temperature object in Moonraker status, if any.
    # Common forms: "heater_generic chamber", "temperature_sensor chamber".
    chamber_object: str | None = None
    macros: KlipperMacros = field(default_factory=KlipperMacros)
    experimental: bool = True  # False only once verified against real hardware


# ---------------------------------------------------------------------------
# Registry — add new printers here.
# ---------------------------------------------------------------------------
KLIPPER_PROFILES: dict[str, KlipperProfile] = {
    "voron_2.4_350": KlipperProfile(
        key="voron_2.4_350",
        label="Voron 2.4 (350mm)",
        bed_size_mm=(350, 350),
        extruder_count=1,
        has_chamber=True,
        chamber_object="temperature_sensor chamber",
        macros=KlipperMacros(),  # stock Voron defaults
        experimental=False,  # the one profile we test against real hardware
    ),
}

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
