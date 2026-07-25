"""Per-printer capability + transport resolution.

The app was originally Bambu-only; many routes and services assume every
printer speaks MQTT and has AMS, k-profiles, drying, cloud profiles, etc. With
external printers (Klipper/Moonraker, OctoPrint, PrusaLink, …) in the mix those
assumptions must be gated.

This module is the single source of truth. Given a ``Printer`` row it returns:
- ``capabilities_for()`` — a ``PrinterCapabilities`` describing which feature
  families apply (callers gate Bambu-only behaviour on these).
- transport helpers (``is_bambu`` / ``is_external`` / ``is_klipper`` /
  ``is_octoprint``) + ``transport_of()`` — which client implementation to use.

It duck-types the printer object (reads attributes) so it has no import-time
dependency on the SQLAlchemy model and stays free of circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.klipper.profiles import KlipperProfile, get_profile

# ``Printer.connection_type`` values.
CONNECTION_BAMBU = "bambu"
CONNECTION_KLIPPER = "klipper"  # Moonraker (Klipper) transport
CONNECTION_OCTOPRINT = "octoprint"  # OctoPrint REST transport
CONNECTION_PRUSALINK = "prusalink"  # PrusaLink (OctoPrint-compatible) transport
CONNECTION_DUET = "duet"  # Duet / RepRapFirmware (DWC) transport
CONNECTION_FLASHFORGE = "flashforge"  # FlashForge legacy TCP transport
CONNECTION_MKS = "mks"  # MKS WiFi module (HTTP upload + TCP console) transport
CONNECTION_OBICO = "obico"  # Obico cloud relay (upload-only) transport
CONNECTION_SNAPMAKER = "snapmaker"  # Snapmaker 2.0 / Artisan / J1 LAN HTTP transport

# Transport families — which client implementation drives the connection.
MOONRAKER_TYPES = frozenset({CONNECTION_KLIPPER})
OCTOPRINT_TYPES = frozenset({CONNECTION_OCTOPRINT, CONNECTION_PRUSALINK})
DUET_TYPES = frozenset({CONNECTION_DUET})
FLASHFORGE_TYPES = frozenset({CONNECTION_FLASHFORGE})
MKS_TYPES = frozenset({CONNECTION_MKS})
OBICO_TYPES = frozenset({CONNECTION_OBICO})
SNAPMAKER_TYPES = frozenset({CONNECTION_SNAPMAKER})
NON_BAMBU_TYPES = MOONRAKER_TYPES | OCTOPRINT_TYPES | DUET_TYPES | FLASHFORGE_TYPES | MKS_TYPES | OBICO_TYPES | SNAPMAKER_TYPES
# All connection types the API/schema accepts.
KNOWN_CONNECTION_TYPES = frozenset({CONNECTION_BAMBU}) | NON_BAMBU_TYPES

# Transport identifiers returned by ``transport_of``.
TRANSPORT_BAMBU = "bambu"
TRANSPORT_MOONRAKER = "moonraker"
TRANSPORT_OCTOPRINT = "octoprint"
TRANSPORT_DUET = "duet"
TRANSPORT_FLASHFORGE = "flashforge"
TRANSPORT_MKS = "mks"
TRANSPORT_OBICO = "obico"
TRANSPORT_SNAPMAKER = "snapmaker"


@dataclass(frozen=True)
class PrinterCapabilities:
    connection_type: str
    # Feature families. Bambu printers have the full set (finer model-specific
    # gating still lives in printer_manager.supports_* helpers); external
    # printers only have the live-control basics.
    has_ams: bool
    has_drying: bool
    has_kprofiles: bool
    has_cloud: bool
    has_3mf_archive: bool
    has_chamber: bool
    can_control: bool  # pause/resume/stop, set temps, home
    can_print: bool  # upload + start a job
    # Klipper-only geometry/profile context (None otherwise).
    klipper_profile: KlipperProfile | None = None

    @property
    def is_bambu(self) -> bool:
        return self.connection_type == CONNECTION_BAMBU

    @property
    def is_klipper(self) -> bool:
        return self.connection_type in MOONRAKER_TYPES

    @property
    def is_external(self) -> bool:
        return self.connection_type != CONNECTION_BAMBU


def _connection_type(printer) -> str:
    # Pre-migration rows / older callers may not have the attribute yet.
    return getattr(printer, "connection_type", None) or CONNECTION_BAMBU


def capabilities_for(printer) -> PrinterCapabilities:
    """Resolve the capability set for a printer row."""
    conn = _connection_type(printer)

    if conn in MOONRAKER_TYPES:
        profile = get_profile(getattr(printer, "model", None))
        return PrinterCapabilities(
            connection_type=conn,
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

    if conn in OCTOPRINT_TYPES or conn in DUET_TYPES or conn in FLASHFORGE_TYPES or conn in MKS_TYPES or conn in SNAPMAKER_TYPES:
        # OctoPrint / PrusaLink / Duet / FlashForge / MKS: live control +
        # upload, no Bambu feature families. Chamber is config/plugin-dependent
        # and not modelled yet.
        return PrinterCapabilities(
            connection_type=conn,
            has_ams=False,
            has_drying=False,
            has_kprofiles=False,
            has_cloud=False,
            has_3mf_archive=False,
            has_chamber=False,
            can_control=True,
            can_print=True,
            klipper_profile=None,
        )

    if conn in OBICO_TYPES:
        # Obico: upload-only relay — no live status, no pause/resume/stop/gcode/
        # light API (see services/obico/__init__.py for why). can_control is
        # False here, unlike every other external transport.
        return PrinterCapabilities(
            connection_type=conn,
            has_ams=False,
            has_drying=False,
            has_kprofiles=False,
            has_cloud=False,
            has_3mf_archive=False,
            has_chamber=False,
            can_control=False,
            can_print=True,
            klipper_profile=None,
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


def transport_of(printer) -> str:
    """Which client transport drives this printer (bambu / moonraker / octoprint)."""
    conn = _connection_type(printer)
    if conn in MOONRAKER_TYPES:
        return TRANSPORT_MOONRAKER
    if conn in OCTOPRINT_TYPES:
        return TRANSPORT_OCTOPRINT
    if conn in DUET_TYPES:
        return TRANSPORT_DUET
    if conn in FLASHFORGE_TYPES:
        return TRANSPORT_FLASHFORGE
    if conn in MKS_TYPES:
        return TRANSPORT_MKS
    if conn in SNAPMAKER_TYPES:
        return TRANSPORT_SNAPMAKER
    if conn in OBICO_TYPES:
        return TRANSPORT_OBICO
    return TRANSPORT_BAMBU


def is_bambu(printer) -> bool:
    return _connection_type(printer) == CONNECTION_BAMBU


def is_external(printer) -> bool:
    """Any non-Bambu printer (Klipper, OctoPrint, PrusaLink, …).

    This is the correct gate for 'Bambu-only feature' guards — it catches every
    external transport, not just Klipper.
    """
    return _connection_type(printer) != CONNECTION_BAMBU


def is_klipper(printer) -> bool:
    """Moonraker-transport printers (Klipper)."""
    return _connection_type(printer) in MOONRAKER_TYPES


def is_octoprint(printer) -> bool:
    """OctoPrint-transport printers (OctoPrint, PrusaLink)."""
    return _connection_type(printer) in OCTOPRINT_TYPES
