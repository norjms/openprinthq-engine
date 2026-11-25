"""Sanity checks for the Klipper printer profile registry.

Guards the curated printer list against typos / inconsistent entries as it
grows (Chunk 0 and beyond).
"""

from backend.app.services.klipper.profiles import (
    DEFAULT_KLIPPER_PROFILE_KEY,
    KLIPPER_PROFILES,
    get_profile,
    list_profiles,
)


def test_registry_is_non_trivial():
    # We expanded well past the original Voron-only set.
    assert len(KLIPPER_PROFILES) >= 30


def test_default_key_exists():
    assert DEFAULT_KLIPPER_PROFILE_KEY in KLIPPER_PROFILES


def test_every_profile_is_well_formed():
    for key, p in KLIPPER_PROFILES.items():
        assert p.key == key, f"{key}: key mismatch ({p.key})"
        assert p.label.strip(), f"{key}: empty label"
        x, y = p.bed_size_mm
        assert 50 <= x <= 1000 and 50 <= y <= 1000, f"{key}: implausible bed {p.bed_size_mm}"
        assert p.extruder_count >= 1
        # leveling_label must be present iff a levelling macro exists.
        has_macro = p.macros.level is not None
        has_label = p.leveling_label is not None
        assert has_macro == has_label, f"{key}: leveling macro/label mismatch"


def test_labels_are_unique():
    labels = [p.label for p in list_profiles()]
    assert len(labels) == len(set(labels)), "duplicate profile labels"


def test_keys_are_unique_and_slug_like():
    for key in KLIPPER_PROFILES:
        assert key == key.lower(), f"{key}: keys should be lowercase"
        assert " " not in key, f"{key}: keys should not contain spaces"


def test_get_profile_falls_back_for_unknown():
    p = get_profile("totally_unknown_printer_xyz")
    assert p.key == DEFAULT_KLIPPER_PROFILE_KEY
