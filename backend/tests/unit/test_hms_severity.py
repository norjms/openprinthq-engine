"""Bambu HMS severity and catalog lookups.

An ``hms[]`` entry is two 32-bit words. Bambu Studio (``DevHMSItem::parse_hms_info``)
reads them as: ``attr`` = module id (bits 24-31), module number (16-23), part id
(8-15), reserved (0-7); ``code`` = message level (bits 16-31: 1 fatal, 2 serious,
3 common, 4 info) and message code (0-15). The long code Bambu publishes is
``MMNNPPRR 00LL CCCC``.

An H2C printing normally reported 0C00_0100_0002_0015 (XCam module, toolhead
camera part, level 2) and the engine graded it fatal, because it took the
severity from the part id (attr bits 8-11) instead of the level.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services import alarm_feed as af
from backend.app.services.hms_errors import get_error_description


@pytest.fixture
def client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    return BambuMQTTClient(ip_address="127.0.0.1", serial_number="31B00TEST000001", access_code="12345678")


def _severities(client, entries):
    client._update_state({"hms": entries})
    return [e.severity for e in client.state.hms_errors]


class TestHMSLevelDecoding:
    def test_h2c_0c00_0015_is_serious_not_fatal(self, client):
        # HMS_0C00_0100_0002_0015: part id 0x01 used to be read as "fatal".
        assert _severities(client, [{"attr": 0x0C000100, "code": 0x00020015}]) == [2]

    @pytest.mark.parametrize(
        ("attr", "code", "level"),
        [
            (0x03000200, 0x00010001, 1),  # part 0x02, level fatal
            (0x0C000300, 0x00030008, 3),  # part 0x03, level common
            (0x0C000100, 0x00040001, 4),  # part 0x01, level info
            (0x07012000, 0x00020009, 2),  # part 0x20 (low nibble 0)
        ],
    )
    def test_level_comes_from_code_not_attr(self, client, attr, code, level):
        assert _severities(client, [{"attr": attr, "code": code}]) == [level]

    def test_string_payload_is_decoded_the_same(self, client):
        assert _severities(client, [{"attr": "0x0C000100", "code": "0x00020015"}]) == [2]

    def test_unknown_level_falls_back_to_serious(self, client):
        # No level in the high half (and levels past info) are HMS_UNKNOWN in
        # Bambu Studio; keep treating them as worth attention.
        assert _severities(client, [{"attr": 0x07FF0100, "code": 0x8011}]) == [2]
        assert _severities(client, [{"attr": 0x07FF0100, "code": 0x00078011}]) == [2]

    def test_alarm_feed_grades_the_h2c_code_serious_with_text(self, client):
        client._update_state({"hms": [{"attr": 0x0C000100, "code": 0x00020015}]})
        state = SimpleNamespace(connected=True, state="RUNNING", hms_errors=client.state.hms_errors, raw_data={})
        alarms = af.active_alarms(state, "bambu")
        assert [(a.code, a.severity) for a in alarms] == [("0C00_0015", "serious")]
        assert alarms[0].message != "HMS 0C00_0015"
        assert "camera" in alarms[0].message.lower()


class TestCatalog:
    def test_long_code_wins_over_short(self):
        assert "toolhead camera" in get_error_description("0C00_0015", "0C00010000020015").lower()
        assert "Laser Protective Insert" in get_error_description("0C00_0015", "0C00040000010015")
        assert "Foreign object detection" in get_error_description("0C00_0015", "0C00030000020015")

    def test_short_0c00_0015_alone_is_ambiguous(self):
        # Three different HMS codes share the short form 0C00_0015.
        assert get_error_description("0C00_0015") is None

    def test_short_lookup_unchanged_without_full_code(self):
        assert get_error_description("0300_400C") == "The task was canceled."
        assert get_error_description("0300_400c") == "The task was canceled."

    def test_long_code_miss_falls_back_to_short(self):
        assert get_error_description("0300_400C", "0300030000004000") == "The task was canceled."

    @pytest.mark.parametrize(
        ("code", "needle"),
        [
            ("0300_8070", "chamber heater temperature is rising abnormally"),
            ("0500_8098", "No material detected"),
        ],
    )
    def test_codes_with_actions_now_have_text(self, code, needle):
        text = get_error_description(code)
        assert text
        assert needle in text

    def test_every_action_code_with_published_text_is_catalogued(self):
        import json
        from pathlib import Path

        from backend.app.services import hms_errors

        actions = json.loads((Path(hms_errors.__file__).resolve().parents[1] / "data" / "hms_actions.json").read_text())
        short = {f"{k[:4]}_{k[4:]}".upper() for codes in actions.values() for k in codes if len(k) == 8}
        longs = {k.upper() for codes in actions.values() for k in codes if len(k) == 16}
        missing_short = sorted(short - set(hms_errors.HMS_ERROR_DESCRIPTIONS) - hms_errors.HMS_UNPUBLISHED_CODES)
        missing_long = sorted(longs - set(hms_errors.HMS_LONG_DESCRIPTIONS))
        assert missing_short == []
        assert missing_long == []


class TestNotificationGrade:
    @pytest.mark.parametrize(("severity", "notify"), [(1, True), (2, True), (3, True), (4, False), (0, False)])
    def test_fatal_serious_and_common_notify_info_does_not(self, severity, notify):
        from backend.app.main import _hms_severity_notifies

        assert _hms_severity_notifies(severity) is notify
