#!/usr/bin/env python3
"""Write the engine's OpenAPI document to docs/openapi.json.

Run from the repository root after changing any API route:

    python scripts/export_openapi.py

backend/tests/unit/test_openapi_spec.py fails while the committed file is out of
date. ``info.version`` is left out of the file so a version bump alone does not
count as a change.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TESTING", "1")


def spec() -> dict:
    from backend.app.main import app

    s = json.loads(json.dumps(app.openapi()))
    s.get("info", {}).pop("version", None)
    return s


def main() -> None:
    out = ROOT / "docs" / "openapi.json"
    out.write_text(json.dumps(spec(), indent=1, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
