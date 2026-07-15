"""Per-printer capability resolution.

The app was originally Bambu-only; many routes and services assume every
printer speaks MQTT and has AMS, k-profiles, drying, cloud profiles, etc.
With Klipper/Moonraker printers in the mix, those assumptions must be gated.

This module is the single source of truth: given a ``Printer`` row, it returns
a ``PrinterCapabilities`` describing which feature families apply. Callers gate
Bambu-only behaviour on these flags (or the ``is_klipper`` shortcut) instead of
checking ``model`` strings ad hoc.

It duck-types the printer object (reads attributes) so it has no import-time
dependency on the SQLAlchemy model and stays free of circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.klipper.profiles import KlipperProfile, get_profile

# Connection type values stored in ``Printer.connection_type``.
CONNECTION_BAMBU = "bambu"
CONNECTION_KLIPPER = "klipper"


@dataclass(frozen=True)
class PrinterCapabilities:
    connection_type: str
    # Feature families. Bambu printers have the full set (finer model-specific
    # gating still lives in printer_manager.supports_* helpers); Klipper
    # printers only have the live-control basics.
    has_ams: bool
    has_drying: bool
    has_kprofiles: bool
    has_cloud: bool
    has_3mf_archive: bool
    has_chamber: bool
    can_control: bool  # pause/resume/stop, set temps, home
    can_print: bool  # upload + start a job
    # Klipper-only geometry/profile context (None for Bambu).
    klipper_profile: KlipperProfile | None = None

    @property
    def is_klipper(self) -> bool:
        return self.connection_type == CONNECTION_KLIPPER

    @property
    def is_bambu(self) -> bool:
        return self.connection_type == CONNECTION_BAMBU


def _connection_type(printer) -> str:
    # Pre-migration rows / older callers may not have the attribute yet.
    return getattr(printer, "connection_type", None) or CONNECTION_BAMBU


def capabilities_for(printer) -> PrinterCapabilities:
    """Resolve the capability set for a printer row."""
    conn = _connection_type(printer)

    if conn == CONNECTION_KLIPPER:
        profile = get_profile(getattr(printer, "model", None))
        return PrinterCapabilities(
            connection_type=CONNECTION_KLIPPER,
            has_ams=False,
            has_drying=False,
            has_kprofiles=False,
            has_cloud=False,
            has_3mf_archive=False,
            has_chamber=profile.has_chamber,
            can_control=True,
            can_print=True,
            klipper_profile=profile,
        )

    # Default: full Bambu capability set.
    return PrinterCapabilities(
        connection_type=CONNECTION_BAMBU,
        has_ams=True,
        has_drying=True,
        has_kprofiles=True,
        has_cloud=True,
        has_3mf_archive=True,
        has_chamber=True,
        can_control=True,
        can_print=True,
        klipper_profile=None,
    )


def is_klipper(printer) -> bool:
    """Convenience shortcut for the most common gate."""
    return _connection_type(printer) == CONNECTION_KLIPPER
