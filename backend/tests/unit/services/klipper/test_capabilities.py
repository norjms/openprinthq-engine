"""Tests for per-printer capability resolution."""

import types

from backend.app.services.printer_capabilities import (
    CONNECTION_BAMBU,
    CONNECTION_KLIPPER,
    capabilities_for,
    is_klipper,
)


def _printer(**kw):
    return types.SimpleNamespace(**kw)


def test_bambu_printer_has_full_capabilities():
    p = _printer(connection_type="bambu", model="X1C")
    caps = capabilities_for(p)
    assert caps.is_bambu and not caps.is_klipper
    assert caps.has_ams and caps.has_drying and caps.has_kprofiles
    assert caps.has_cloud and caps.has_3mf_archive
    assert caps.klipper_profile is None
    assert is_klipper(p) is False


def test_klipper_printer_has_restricted_capabilities():
    p = _printer(connection_type="klipper", model="voron_2.4_350")
    caps = capabilities_for(p)
    assert caps.is_klipper and not caps.is_bambu
    assert not caps.has_ams
    assert not caps.has_drying
    assert not caps.has_kprofiles
    assert not caps.has_cloud
    assert not caps.has_3mf_archive
    assert caps.has_chamber  # Voron 2.4 profile has a chamber
    assert caps.can_control and caps.can_print
    assert caps.klipper_profile is not None
    assert caps.klipper_profile.bed_size_mm == (350, 350)
    assert is_klipper(p) is True


def test_missing_connection_type_defaults_to_bambu():
    # Pre-migration rows / older callers without the attribute.
    p = _printer(model="P1S")
    caps = capabilities_for(p)
    assert caps.connection_type == CONNECTION_BAMBU
    assert caps.has_ams


def test_unknown_klipper_profile_falls_back_to_default():
    p = _printer(connection_type=CONNECTION_KLIPPER, model="some_unknown_printer")
    caps = capabilities_for(p)
    # Falls back to the default profile rather than crashing.
    assert caps.is_klipper
    assert caps.klipper_profile is not None
