"""Rebuild backend/app/data/hms_texts.json from Bambu Studio's HMS catalogs.

Bambu Studio ships one English catalog per printer family in resources/hms
(hms_en_<model>.json). Each has ``device_hms`` (16-character codes, the
``hms[]`` entries) and ``device_error`` (8-character ``print_error`` codes).
This keeps the text of every code that has any, the first catalog in MODELS
that has it winning, so the alarm feed can say what any code means.

Usage: python scripts/update_hms_texts.py [catalog_dir]
With no directory the catalogs are fetched from GitHub.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# H2D and H2C families first, then the rest.
MODELS = ("094", "093", "239", "31B", "20P", "22E", "26A", "32X")
URL = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/hms/hms_en_{}.json"
OUT = Path(__file__).resolve().parent.parent / "backend" / "app" / "data" / "hms_texts.json"


def _load(model: str, src: Path | None) -> dict:
    if src is not None:
        return json.loads((src / f"hms_en_{model}.json").read_text(encoding="utf-8"))
    with urllib.request.urlopen(URL.format(model), timeout=60) as r:  # noqa: S310 (fixed https URL)
        return json.load(r)


def _entries(section) -> list[dict]:
    if isinstance(section, dict):
        section = section.get("en") or []
    return section or []


def build(src: Path | None = None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"hms": {}, "print_error": {}}
    for model in MODELS:
        data = _load(model, src).get("data") or {}
        for key, section in (("hms", "device_hms"), ("print_error", "device_error")):
            for e in _entries(data.get(section)):
                code = str(e.get("ecode") or "").upper()
                text = " ".join(str(e.get("intro") or "").split())
                if code and text and code not in out[key]:
                    out[key][code] = text
    return {k: dict(sorted(v.items())) for k, v in out.items()}


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    data = build(src)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(data['hms'])} hms codes, {len(data['print_error'])} print_error codes")


if __name__ == "__main__":
    main()
