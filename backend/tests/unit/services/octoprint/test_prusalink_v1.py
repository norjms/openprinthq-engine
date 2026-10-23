"""PrusaLink: storage detection for uploads and native v1 job control.

Every request goes to an httpx.MockTransport; nothing reaches a printer.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.app.services.octoprint import octoprint_client, octoprint_files
from backend.app.services.octoprint.octoprint_client import PrusaLinkClient


def _patch_async_client(monkeypatch, module, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)


async def test_upload_uses_the_printers_storage(monkeypatch, tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers.get("Print-After-Upload")))
        if req.url.path == "/api/v1/storage":
            return httpx.Response(
                200,
                json={"storage_list": [{"path": "/usb/", "name": "usb", "read_only": False, "available": True}]},
            )
        return httpx.Response(201)

    _patch_async_client(monkeypatch, octoprint_files, handler)
    f = tmp_path / "part.bgcode"
    f.write_bytes(b"GCDE")
    name = await octoprint_files.upload_gcode(
        "192.0.2.5", 80, str(f), api_key="k", flavor="prusalink", start_print=True
    )
    assert name == "part.bgcode"
    assert ("PUT", "/api/v1/files/usb/part.bgcode", "1") in seen


async def test_storage_skips_read_only_and_falls_back(monkeypatch):
    def handler(req):
        return httpx.Response(
            200,
            json={
                "storage_list": [
                    {"path": "/sd/", "read_only": True, "available": True},
                    {"path": "/local/", "read_only": False, "available": True},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await octoprint_files.prusalink_storage(c, "http://192.0.2.5", None) == "local"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        assert await octoprint_files.prusalink_storage(c, "http://192.0.2.5", None) == "usb"


async def test_explicit_storage_is_not_looked_up(monkeypatch, tmp_path):
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(201)

    _patch_async_client(monkeypatch, octoprint_files, handler)
    f = tmp_path / "a.gcode"
    f.write_bytes(b"G28")
    await octoprint_files.upload_gcode("192.0.2.5", 80, str(f), flavor="prusalink", storage="local")
    assert seen == ["/api/v1/files/local/a.gcode"]


@pytest.mark.parametrize(
    "action,method,path",
    [
        ("pause", "PUT", "/api/v1/job/42/pause"),
        ("resume", "PUT", "/api/v1/job/42/resume"),
        ("stop", "DELETE", "/api/v1/job/42"),
    ],
)
async def test_job_control_uses_v1_job_id(monkeypatch, action, method, path):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/v1/job":
            return httpx.Response(200, json={"id": 42, "state": "PRINTING"})
        return httpx.Response(204)

    _patch_async_client(monkeypatch, octoprint_client, handler)
    c = PrusaLinkClient("192.0.2.5", 80, api_key="k", serial_number="MK4S")
    c._loop = asyncio.get_running_loop()
    assert getattr(c, f"{action}_print")() is True
    for _ in range(50):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    assert calls == [("GET", "/api/v1/job"), (method, path)]


async def test_no_job_sends_nothing_else(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(204)

    _patch_async_client(monkeypatch, octoprint_client, handler)
    c = PrusaLinkClient("192.0.2.5", 80, serial_number="MK4S")
    c._loop = asyncio.get_running_loop()
    assert c.stop_print() is True
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert calls == ["/api/v1/job"]
