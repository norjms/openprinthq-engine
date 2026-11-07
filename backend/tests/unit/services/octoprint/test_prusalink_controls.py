"""PrusaLink: how a print ended, the ATTENTION state, and the controls it lacks.

Every request goes to an httpx.MockTransport; nothing reaches a printer.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend.app.api.routes.printers import _ensure_client_supports, _set_heater
from backend.app.services.octoprint.octoprint_client import OctoPrintClient, PrusaLinkClient
from backend.app.services.printer_capabilities import capabilities_for


def _client(cls=PrusaLinkClient):
    done = []
    c = cls("192.0.2.5", 80, api_key="k", serial_number="t", on_print_complete=done.append)
    return c, done


def _status(state: str, progress: int | None = None) -> dict:
    body = {"printer": {"state": state, "temp_nozzle": 200.0, "target_nozzle": 0, "temp_bed": 60.0, "target_bed": 0}}
    if progress is not None:
        body["job"] = {"id": 7, "progress": progress, "file": {"display_name": "part.bgcode"}}
    return body


async def _poll(client, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as http:
        await client._poll_once(http)


@pytest.mark.parametrize(
    ("end_state", "last_progress", "expected"),
    [
        ("FINISHED", 97, "completed"),  # printer says finished even if the last poll was short of 99
        ("STOPPED", 100, "cancelled"),  # cancelled on the printer at the very end
        ("IDLE", 100, "completed"),  # no explicit state: fall back to progress
        ("IDLE", 40, "cancelled"),
    ],
)
async def test_finish_status_follows_prusalink_state(end_state, last_progress, expected):
    c, done = _client()
    await _poll(c, _status("PRINTING", last_progress))
    await _poll(c, _status(end_state))
    assert [d["status"] for d in done] == [expected]


async def test_attention_is_a_pause_not_a_failure():
    c, done = _client()
    await _poll(c, _status("PRINTING", 30))
    await _poll(c, _status("ATTENTION", 30))
    assert c.state.state == "PAUSE"
    assert done == []
    await _poll(c, _status("PRINTING", 31))
    await _poll(c, _status("FINISHED"))
    assert [d["status"] for d in done] == ["completed"]


async def test_error_still_fails_the_print():
    c, done = _client()
    await _poll(c, _status("PRINTING", 30))
    await _poll(c, _status("ERROR"))
    assert [d["status"] for d in done] == ["failed"]


def test_prusalink_declares_and_refuses_the_controls_it_lacks():
    c, _ = _client()
    assert {"send_gcode", "set_nozzle_temp", "set_bed_temp", "home"} <= c.UNSUPPORTED_METHODS
    # Never scheduled: no loop is needed because nothing is sent.
    assert c.send_gcode("G28") is False
    assert c.set_nozzle_temp(200) is False
    assert c.set_bed_temp(60) is False
    assert c.home() is False
    for method in ("send_gcode", "set_nozzle_temp", "home"):
        with pytest.raises(HTTPException) as e:
            _ensure_client_supports(c, method, "x")
        assert e.value.status_code == 400
        assert e.value.detail["code"] == "unsupported_for_printer_type"
    # Pause, resume and stop stay available.
    _ensure_client_supports(c, "pause_print", "Pause")


def test_octoprint_keeps_its_controls():
    c, _ = _client(OctoPrintClient)
    assert getattr(c, "UNSUPPORTED_METHODS", frozenset()) == frozenset()
    _ensure_client_supports(c, "send_gcode", "G-code console")


def test_set_heater_uses_the_generic_methods_on_external_clients():
    calls = []
    client = SimpleNamespace(
        set_nozzle_temp=lambda t: calls.append(("nozzle", t)) or True,
        set_bed_temp=lambda t: calls.append(("bed", t)) or True,
    )
    assert _set_heater(client, "nozzle", 210) is True
    assert _set_heater(client, "bed", 60) is True
    assert calls == [("nozzle", 210), ("bed", 60)]
    with pytest.raises(HTTPException) as e:
        _set_heater(client, "nozzle", 210, nozzle=1)
    assert e.value.status_code == 400


def test_set_heater_prefers_the_bambu_methods():
    calls = []
    client = SimpleNamespace(
        set_nozzle_temperature=lambda t, n: calls.append(("nozzle", t, n)) or True,
        set_bed_temperature=lambda t: calls.append(("bed", t)) or True,
    )
    assert _set_heater(client, "nozzle", 220, 1) is True
    assert _set_heater(client, "bed", 55) is True
    assert calls == [("nozzle", 220, 1), ("bed", 55)]


def test_set_heater_refuses_prusalink():
    c, _ = _client()
    with pytest.raises(HTTPException) as e:
        _set_heater(c, "bed", 60)
    assert e.value.status_code == 400


def test_set_heater_refuses_a_client_with_no_heater_methods():
    with pytest.raises(HTTPException) as e:
        _set_heater(SimpleNamespace(), "nozzle", 200)
    assert e.value.status_code == 400


@pytest.mark.parametrize(
    ("conn", "home", "temperature", "gcode"),
    [
        ("prusalink", False, False, False),
        ("octoprint", True, True, True),
        ("klipper", True, True, True),
        ("bambu", True, True, True),
        ("obico", False, False, False),
    ],
)
def test_controls_per_connection_type(conn, home, temperature, gcode):
    controls = capabilities_for(SimpleNamespace(connection_type=conn, model=None)).controls()
    assert (controls["home"], controls["temperature"], controls["gcode"]) == (home, temperature, gcode)
    assert controls["pause_resume_stop"] is (conn != "obico")


async def test_printer_api_returns_controls(async_client, printer_factory):
    """Both the list and the detail route carry controls (they build the response with model_validate)."""
    bambu = await printer_factory(name="Bambu")
    prusa = await printer_factory(
        name="MK4S", connection_type="prusalink", serial_number="prusalink:test", access_code="", model=None
    )
    r = await async_client.get("/api/v1/printers/")
    assert r.status_code == 200, r.text
    by = {p["name"]: p["controls"] for p in r.json()}
    assert by["Bambu"]["home"] is True and by["Bambu"]["temperature"] is True
    assert by["MK4S"] == {"pause_resume_stop": True, "print": True, "home": False, "temperature": False, "gcode": False}
    one = (await async_client.get(f"/api/v1/printers/{prusa.id}")).json()
    assert one["controls"]["home"] is False
    assert (await async_client.get(f"/api/v1/printers/{bambu.id}")).json()["controls"]["gcode"] is True
