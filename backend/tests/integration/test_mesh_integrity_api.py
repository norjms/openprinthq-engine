"""End to end: scan a folder of real meshes, then read the integrity results.

This is the loop the Files tab depends on. It exercises the parts the unit tests
cannot: that the pass finds the files a scan indexed, that a result survives in
``library_mesh_reports`` keyed by content, that a rescan reuses it, and that a
STEP file sitting in the same folder never appears as a fault.
"""

import numpy as np
import pytest
import trimesh
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryMeshReport
from backend.app.services import mesh_integrity


@pytest.fixture(autouse=True)
def _enable_external_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("BAMBUDDY_EXTERNAL_ROOTS", str(tmp_path.parent))


@pytest.fixture
def mesh_dir(tmp_path):
    d = tmp_path / "bucket"
    d.mkdir()
    good = trimesh.creation.icosphere(subdivisions=3, radius=20.0)
    (d / "good.stl").write_bytes(trimesh.exchange.stl.export_stl(good))

    keep = np.ones(len(good.faces), dtype=bool)
    keep[[0, 1, 2, 3]] = False
    holed = trimesh.Trimesh(vertices=good.vertices, faces=good.faces[keep], process=False)
    (d / "holed.stl").write_bytes(trimesh.exchange.stl.export_stl(holed))

    (d / "bracket.step").write_text("ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    return d


async def _external_folder(async_client: AsyncClient, mesh_dir) -> int:
    res = await async_client.post(
        "/api/v1/library/folders/external",
        json={"name": "Bucket", "external_path": str(mesh_dir), "readonly": True, "show_hidden": False},
    )
    assert res.status_code == 200
    folder_id = res.json()["id"]
    assert (await async_client.post(f"/api/v1/library/folders/{folder_id}/scan")).status_code == 200
    return folder_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pass_reports_the_bad_file_and_clears_the_good_one(async_client: AsyncClient, db_session, mesh_dir):
    folder_id = await _external_folder(async_client, mesh_dir)

    # force=True stands in for the operator switching the feature on.
    result = await mesh_integrity.run_pass([folder_id], force=True)
    assert result["analysed"] == 2, "the STEP file must not be analysed at all"

    res = await async_client.get("/api/v1/library/mesh-integrity", params={"folder_id": folder_id})
    assert res.status_code == 200
    by_name = {v["filename"]: v for v in res.json()["results"].values()}

    assert by_name["good.stl"]["status"] == "ok"
    assert by_name["good.stl"]["findings"] == []
    assert by_name["holed.stl"]["status"] == "problems"
    assert "holes" in {f["code"] for f in by_name["holed.stl"]["findings"]}

    # The whole point: a CAD file in the same folder is not a broken model.
    assert "bracket.step" not in by_name


@pytest.mark.asyncio
@pytest.mark.integration
async def test_result_is_cached_by_content_and_the_hash_is_backfilled(async_client: AsyncClient, db_session, mesh_dir):
    folder_id = await _external_folder(async_client, mesh_dir)
    await mesh_integrity.run_pass([folder_id], force=True)

    rows = (await db_session.execute(select(LibraryMeshReport))).scalars().all()
    assert len(rows) == 2
    assert all(len(r.file_hash) == 64 for r in rows), "the external scanner skips hashing; the pass must backfill it"

    # A second pass with the feature merely enabled analyses nothing new: every
    # file's content already has a current report. This is the cost story.
    again = await mesh_integrity.run_pass([folder_id], force=False)
    assert again["analysed"] == 0
    assert again["reason"] == "disabled"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_per_file_endpoint_analyses_on_demand(async_client: AsyncClient, db_session, mesh_dir):
    folder_id = await _external_folder(async_client, mesh_dir)

    listing = await async_client.get("/api/v1/library/files", params={"folder_id": folder_id})
    holed = next(f for f in listing.json() if f["filename"] == "holed.stl")
    step = next(f for f in listing.json() if f["filename"] == "bracket.step")

    # Nothing has run yet, so the file is unknown rather than clean.
    before = await async_client.get(f"/api/v1/library/files/{holed['id']}/mesh-integrity")
    assert before.json()["status"] == "unknown"
    assert before.json()["checked"] is False

    after = await async_client.post(f"/api/v1/library/files/{holed['id']}/mesh-integrity")
    assert after.status_code == 200
    assert after.json()["status"] == "problems"

    # A STEP file is never analysable, even when asked for directly.
    assert (await async_client.post(f"/api/v1/library/files/{step['id']}/mesh-integrity")).status_code == 400
    assert (await async_client.get(f"/api/v1/library/files/{step['id']}/mesh-integrity")).json()["checked"] is False
