"""Bambu HMS severity and catalog lookups.

An ``hms[]`` entry is two 32-bit words. Bambu Studio (``DevHMSItem::parse_hms_info``)
reads them as: ``attr`` = module id (bits 24-31), module number (16-23), part id
(8-15), reserved (0-7); ``code`` = message level (bits 16-31: 1 fatal, 2 serious,
3 common, 4 info) and message code (0-15). The long code Bambu publishes is
``MMNNPPRR 00LL CCCC``.

An H2C printing normally reported 0C00_0100_0002_0015 (XCam module, toolhead
camera part, level 2) and the engine graded it fatal, because it took the
severity from the part id (attr bits 8-11) instead of the level. Bambu
publishes that code with no text: it is internal, and Bambu Studio does not
show it.
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

    def test_alarm_feed_leaves_out_the_internal_h2c_code(self, client):
        client._update_state({"hms": [{"attr": 0x0C000100, "code": 0x00020015}]})
        assert [e.full_code for e in client.state.hms_errors] == ["0C00010000020015"]
        state = SimpleNamespace(connected=True, state="RUNNING", hms_errors=client.state.hms_errors, raw_data={})
        assert af.active_alarms(state, "bambu") == []


class TestCatalog:
    def test_long_code_wins_over_short(self):
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


class TestH2CProdPayload:
    """The four hms[] entries an H2C reported together during a normal print.

    Before the level fix 0C00_0015 was graded fatal, 0700_0025 serious with no
    text, and the two mainboard entries had "severity" 6 (part id 0x06), which
    the feed dropped. All four are level 2. Bambu publishes text only for
    0700220000020025 (H2C catalog, hms_en_31B.json): the other three are
    internal codes, which Bambu Studio does not show.
    """

    ENTRIES = [
        {"code": "0x20015", "attr": 201326848, "module": 12, "full_code": "0C00010000020015"},
        {"code": "0x20025", "attr": 117449216, "module": 7, "full_code": "0700220000020025"},
        {"code": "0x20005", "attr": 83887616, "module": 5, "full_code": "0500060000020005"},
        {"code": "0x20006", "attr": 83887616, "module": 5, "full_code": "0500060000020006"},
    ]

    def test_parser_grades_all_four_serious(self, client):
        client._update_state({"hms": [dict(e) for e in self.ENTRIES]})
        got = [(e.full_code, e.severity, e.module) for e in client.state.hms_errors]
        assert got == [(e["full_code"], 2, e["module"]) for e in self.ENTRIES]

    def test_feed_raises_only_the_ams_feed_resistance_warning(self, client):
        client._update_state({"hms": [dict(e) for e in self.ENTRIES]})
        state = SimpleNamespace(connected=True, state="RUNNING", hms_errors=client.state.hms_errors, raw_data={})
        alarms = af.active_alarms(state, "bambu")
        assert [(a.code, a.severity) for a in alarms] == [("0700_0025", "serious")]
        assert alarms[0].message.startswith("AMS A slot 3 feed resistance is too high.")

    def test_catalog(self):
        from backend.app.services.hms_errors import is_internal_code

        assert get_error_description("0700_0025", "0700220000020025").startswith("AMS A slot 3 feed resistance")
        assert get_error_description("0700_0025", "0703200000020025").startswith("AMS D slot 1 feed resistance")
        # Four AMS slots share the short form, so it stays out of the short catalog.
        assert get_error_description("0700_0025") is None
        for code in ("0C00010000020015", "0500060000020005", "0500060000020006"):
            assert is_internal_code(code)
            assert get_error_description(f"{code[:4]}_{code[12:]}", code) is None
        assert not is_internal_code("0700220000020025")
        assert not is_internal_code(None)
