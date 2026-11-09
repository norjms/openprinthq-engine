"""Elegoo G-code upload (SDCP multipart packets, CC2 PUT chunks)."""

import hashlib

import httpx
import pytest

from backend.app.services.elegoo import elegoo_files


class _Recorder:
    def __init__(self, responder):
        self.requests = []
        self.responder = responder

    def __call__(self, request):
        self.requests.append(request)
        return self.responder(request)


@pytest.fixture
def patched_client(monkeypatch):
    def install(responder):
        rec = _Recorder(responder)
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(rec)
            return real(*args, **kwargs)

        monkeypatch.setattr(elegoo_files.httpx, "AsyncClient", factory)
        return rec

    return install


async def test_cc2_upload_sends_ranged_chunks(tmp_path, patched_client, monkeypatch):
    monkeypatch.setattr(elegoo_files, "CHUNK_SIZE", 4)
    f = tmp_path / "a.gcode"
    f.write_bytes(b"G28\nG1X1\n")  # 9 bytes -> 3 chunks
    rec = patched_client(lambda r: httpx.Response(200, json={"error_code": 0, "offset": 0}))
    await elegoo_files.upload_cc2("192.0.2.11", str(f), "a.gcode", token=None)
    ranges = [r.headers["Content-Range"] for r in rec.requests]
    assert ranges == ["bytes 0-3/9", "bytes 4-7/9", "bytes 8-8/9"]
    first = rec.requests[0]
    assert first.method == "PUT" and str(first.url) == "http://192.0.2.11/upload"
    assert first.headers["X-Token"] == "123456"
    assert first.headers["X-File-MD5"] == hashlib.md5(b"G28\nG1X1\n").hexdigest()  # noqa: S324


async def test_cc2_upload_stops_on_busy(tmp_path, patched_client):
    f = tmp_path / "a.gcode"
    f.write_bytes(b"G28\n")
    patched_client(lambda r: httpx.Response(429))
    with pytest.raises(RuntimeError, match="429"):
        await elegoo_files.upload_cc2("192.0.2.11", str(f), "a.gcode", token="9999")


async def test_sdcp_upload_posts_multipart_packets(tmp_path, patched_client, monkeypatch):
    monkeypatch.setattr(elegoo_files, "CHUNK_SIZE", 5)
    f = tmp_path / "b.gcode"
    f.write_bytes(b"0123456789AB")
    rec = patched_client(lambda r: httpx.Response(200, json={"code": "000000", "success": True}))
    await elegoo_files.upload_sdcp("192.0.2.10", 3030, str(f), "b.gcode")
    assert len(rec.requests) == 3
    body = rec.requests[1].content.decode("latin-1")
    assert 'name="Offset"' in body and "\r\n5\r\n" in body
    assert 'name="TotalSize"' in body and "\r\n12\r\n" in body
    assert str(rec.requests[0].url) == "http://192.0.2.10:3030/uploadFile/upload"


async def test_sdcp_upload_reports_refusal(tmp_path, patched_client):
    f = tmp_path / "b.gcode"
    f.write_bytes(b"x")
    patched_client(
        lambda r: httpx.Response(
            200, json={"code": "111111", "success": False, "messages": [{"field": "common_field", "message": -2}]}
        )
    )
    with pytest.raises(RuntimeError, match="refused"):
        await elegoo_files.upload_sdcp("192.0.2.10", 3030, str(f), "b.gcode")


async def test_empty_file_is_refused(tmp_path):
    f = tmp_path / "e.gcode"
    f.write_bytes(b"")
    with pytest.raises(ValueError):
        await elegoo_files.upload_cc2("192.0.2.11", str(f), "e.gcode")
