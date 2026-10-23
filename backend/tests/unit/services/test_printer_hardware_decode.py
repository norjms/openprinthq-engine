"""Unit tests for nozzle decoding and filament-path helpers (services/printer_hardware)."""

import pytest

from backend.app.services import printer_hardware as hw
from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def mqtt_client():
    return BambuMQTTClient(ip_address="192.0.2.10", serial_number="TESTNOZZLE", access_code="12345678")


class TestDecodeBambuNozzleType:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HS01", ("hardened_steel", "High Flow")),
            ("HS00", ("stainless_steel", "High Flow")),
            ("SS00", ("stainless_steel", "Standard")),
            ("AS01", ("hardened_steel", "Standard")),
            ("XS05", ("tungsten_carbide", "Standard")),
            ("ES01", ("hardened_steel", "High Flow")),
            ("US01", ("hardened_steel", "TPU High Flow")),
            ("BS00", ("stainless_steel", "E3D High Flow")),
            ("HS", ("undefine", "High Flow")),
            ("hardened_steel", ("hardened_steel", "Standard")),
            ("stainless_steel", ("stainless_steel", "Standard")),
        ],
    )
    def test_known_codes(self, raw, expected):
        assert hw.decode_bambu_nozzle_type(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "N/A", "n/a", "Q", "ZZ99"])
    def test_unset_or_unknown(self, raw):
        assert hw.decode_bambu_nozzle_type(raw) is None

    def test_unknown_material_code_falls_back_to_undefine(self):
        assert hw.decode_bambu_nozzle_type("HS09") == ("undefine", "High Flow")

    def test_live_block_keeps_raw_type(self):
        live = hw.decode_live_nozzle("HS01", "0.6")
        assert live == {
            "diameter_mm": 0.6,
            "nozzle_type": "hardened_steel",
            "volume_type": "High Flow",
            "raw_type": "HS01",
        }

    def test_live_block_none_when_nothing_known(self):
        assert hw.decode_live_nozzle("", "") is None
        assert hw.decode_live_nozzle("N/A", None) is None


class TestMqttNozzleInfoDecode:
    def test_h2d_nozzle_info_decodes_per_extruder(self, mqtt_client):
        payload = {
            "print": {
                "device": {
                    "nozzle": {
                        "info": [
                            {"id": 0, "type": "HS01", "diameter": "0.4"},
                            {"id": 1, "type": "SS00", "diameter": "0.6"},
                        ]
                    }
                }
            }
        }
        mqtt_client._process_message(payload)
        live = hw.live_nozzles_from_state(mqtt_client.state)
        assert live[0]["nozzle_type"] == "hardened_steel"
        assert live[0]["volume_type"] == "High Flow"
        assert live[0]["raw_type"] == "HS01"
        assert live[0]["diameter_mm"] == 0.4
        assert live[1]["nozzle_type"] == "stainless_steel"
        assert live[1]["volume_type"] == "Standard"
        assert live[1]["diameter_mm"] == 0.6

    def test_h2c_rack_excludes_mounted_hotends(self, mqtt_client):
        payload = {
            "print": {
                "device": {
                    "nozzle": {
                        "info": [
                            {"id": 0, "type": "HS01", "diameter": "0.4"},
                            {"id": 1, "type": "HS01", "diameter": "0.4"},
                            {"id": 16, "type": "US01", "diameter": "0.6", "wear": 2, "sn": "R1"},
                            {"id": 17, "type": "N/A", "diameter": ""},
                        ]
                    }
                }
            }
        }
        mqtt_client._process_message(payload)
        rack = hw.rack_from_state(mqtt_client.state)
        assert [r["id"] for r in rack] == [16, 17]
        assert rack[0]["volume_type"] == "TPU High Flow"
        assert rack[0]["serial_number"] == "R1"
        assert rack[1]["nozzle_type"] is None

    def test_single_nozzle_legacy_fields(self, mqtt_client):
        mqtt_client._process_message({"print": {"nozzle_type": "hardened_steel", "nozzle_diameter": "0.4"}})
        live = hw.live_nozzles_from_state(mqtt_client.state)
        assert live == {
            0: {
                "diameter_mm": 0.4,
                "nozzle_type": "hardened_steel",
                "volume_type": "Standard",
                "raw_type": "hardened_steel",
            }
        }


class TestSlotHelpers:
    def test_slot_labels(self):
        assert hw.slot_label(0, 1, 1) == "AMS A slot 2"
        assert hw.slot_label(128, 0, 2) == "HT 1"
        assert hw.slot_label(255, 0, 1) == "External"
        assert hw.slot_label(255, 0, 2) == "External left"
        assert hw.slot_label(255, 1, 2) == "External right"

    def test_external_tray_extruder_follows_vt_convention(self):
        # tray 0 = vt 254 = deputy/left (1); tray 1 = vt 255 = main/right (0)
        assert hw.external_tray_extruder(0, 2) == 1
        assert hw.external_tray_extruder(1, 2) == 0
        assert hw.external_tray_extruder(0, 1) == 0
