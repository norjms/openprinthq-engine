"""The published OpenAPI document: stable ids, security schemes, and no drift."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _spec():
    path = ROOT / "scripts" / "export_openapi.py"
    mod_spec = importlib.util.spec_from_file_location("export_openapi", path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return mod.spec()


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


def test_committed_spec_is_current():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(_spec())), (
        "docs/openapi.json is out of date: run `python scripts/export_openapi.py` and commit the result"
    )
