"""Resumable library uploads: pieces, offsets, resume, completion, limits."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.api.routes import library_uploads


@pytest.fixture(autouse=True)
def _tmp_base(tmp_path, monkeypatch):
    monkeypatch.setattr(library_uploads.app_settings, "base_dir", tmp_path)


async def test_pieces_resume_and_complete(async_client: AsyncClient, monkeypatch):
    data = b"G28\n" + b"G1 X1 Y1\n" * 200
    r = await async_client.post("/api/v1/library/uploads", json={"filename": "cube.gcode", "total_size": len(data)})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await async_client.put(f"/api/v1/library/uploads/{sid}?offset=0", content=data[:1000])
    assert r.status_code == 200 and r.json()["received"] == 1000
    r = await async_client.put(f"/api/v1/library/uploads/{sid}?offset=0", content=data[:10])
    assert r.status_code == 409  # wrong offset
    assert (await async_client.get(f"/api/v1/library/uploads/{sid}")).json()["received"] == 1000
    r = await async_client.post(f"/api/v1/library/uploads/{sid}/complete")
    assert r.status_code == 409  # not all there yet

    seen = {}

    async def fake_upload_file(file, folder_id, generate_stl_thumbnails, db, current_user):
        seen["name"] = file.filename
        seen["data"] = await file.read()
        return {"id": 1, "filename": file.filename}

    import backend.app.api.routes.library as library

    monkeypatch.setattr(library, "upload_file", fake_upload_file)
    r = await async_client.put(f"/api/v1/library/uploads/{sid}?offset=1000", content=data[1000:])
    assert r.json()["received"] == len(data)
    r = await async_client.post(f"/api/v1/library/uploads/{sid}/complete")
    assert r.status_code == 200, r.text
    assert seen == {"name": "cube.gcode", "data": data}
    assert (await async_client.get(f"/api/v1/library/uploads/{sid}")).status_code == 404


async def test_limits(async_client: AsyncClient):
    r = await async_client.post("/api/v1/library/uploads", json={"filename": "../x.gcode", "total_size": 10})
    assert r.status_code == 400
    r = await async_client.post("/api/v1/library/uploads", json={"filename": "a.gcode", "total_size": 4})
    sid = r.json()["id"]
    r = await async_client.put(f"/api/v1/library/uploads/{sid}?offset=0", content=b"too long")
    assert r.status_code == 413
    assert (await async_client.get(f"/api/v1/library/uploads/{sid}")).json()["received"] == 0
    assert (await async_client.delete(f"/api/v1/library/uploads/{sid}")).status_code == 200
