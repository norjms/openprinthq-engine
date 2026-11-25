"""Tests for the Obico client (no I/O)."""

from backend.app.services.obico.obico_client import ObicoClient, normalize_host
from backend.app.services.printer_client import PrinterClient


def test_normalize_host_adds_scheme():
    assert normalize_host("obico.example.com") == "http://obico.example.com"
    assert normalize_host("obico.example.com/") == "http://obico.example.com"


def test_normalize_host_preserves_existing_scheme():
    assert normalize_host("https://obico.example.com") == "https://obico.example.com"
    assert normalize_host("http://obico.example.com/") == "http://obico.example.com"


def test_satisfies_protocol():
    assert isinstance(ObicoClient("obico.example.com", api_key="tok"), PrinterClient)


def test_control_surface_is_all_no_ops():
    # Obico exposes no control API — every one of these must return False,
    # not raise, so gated callers fail safe.
    c = ObicoClient("obico.example.com", api_key="tok")
    assert c.pause_print() is False
    assert c.resume_print() is False
    assert c.stop_print() is False
    assert c.send_gcode("G28") is False
    assert c.set_chamber_light(True) is False
    assert c.start_print("x.gcode") is False


def test_request_status_update_is_noop_true():
    c = ObicoClient("obico.example.com", api_key="tok")
    assert c.request_status_update() is True
