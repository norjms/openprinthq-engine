"""The published OpenAPI document: stable ids, security schemes, and no drift."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMITTED = ROOT / "docs" / "openapi.json"


def _spec():
    # Same as scripts/export_openapi.py spec(); inlined because the Docker test
    # image ships neither scripts/ nor docs/.
    from backend.app.main import app

    s = json.loads(json.dumps(app.openapi()))
    s.get("info", {}).pop("version", None)
    return s


def test_shape():
    s = _spec()
    assert s["openapi"].startswith("3.1")
    ids = []
    for p, item in s["paths"].items():
        assert not (p != "/" and p.endswith("/") and p.rstrip("/") in s["paths"]), p
        for m, op in item.items():
            ids.append(op["operationId"])
            assert op.get("tags"), (m, p)
            assert not any(
                x.get("in") == "header" and x.get("name", "").lower() == "x-api-key" for x in op.get("parameters", [])
            ), (m, p)
    assert len(ids) == len(set(ids))
    assert "library_list_files" in ids
    assert set(s["components"]["securitySchemes"]) == {"bearerAuth", "apiKeyHeader"}
    assert "/{full_path}" not in s["paths"] and "/sw.js" not in s["paths"]


@pytest.mark.skipif(not COMMITTED.exists(), reason="docs/ is not in this checkout (Docker test image)")
def test_committed_spec_is_current():
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(_spec())), (
        "docs/openapi.json is out of date: run `python scripts/export_openapi.py` and commit the result"
    )
