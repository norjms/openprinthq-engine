"""The engine's OpenAPI document, made stable enough to publish and diff.

FastAPI's default document has operationIds built from function name, path and
method (``list_files_api_v1_library_files_get``), lists every slash and
no-slash alias twice, includes the SPA and service-worker routes, and models
API keys as an ordinary header parameter on each operation. This module
post-processes it:

- operationIds are ``<first tag>_<function name>`` (``library_list_files``),
  with the HTTP method appended only if that still collides;
- a path that is only the trailing-slash alias of another is dropped;
- ``X-API-Key`` header parameters are removed and the two ways to
  authenticate are declared once as security schemes (a bearer token, which is
  a session JWT or a ``bb_`` API key, or the ``X-API-Key`` header).

``scripts/export_openapi.py`` writes the result to ``docs/openapi.json``; a test
fails when the committed file no longer matches the code.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "misc"


def build_openapi(app: FastAPI) -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    paths: dict = schema.get("paths", {})

    # Trailing-slash aliases: keep the form without the slash.
    for p in list(paths):
        if p != "/" and p.endswith("/") and p.rstrip("/") in paths:
            del paths[p]

    names: dict[tuple[str, str], tuple[str, str]] = {}
    for r in app.routes:
        if isinstance(r, APIRoute) and r.include_in_schema:
            tag = str(r.tags[0]) if r.tags else "misc"
            for m in r.methods or ():
                names[(r.path_format, m.lower())] = (tag, r.name)

    used: set[str] = set()
    for p in sorted(paths):
        for method in sorted(paths[p]):
            if method not in HTTP_METHODS:
                continue
            op = paths[p][method]
            tag, name = names.get((p, method), ((op.get("tags") or ["misc"])[0], op.get("operationId", "op")))
            oid = f"{_slug(tag)}_{name}"
            if oid in used:
                oid = f"{oid}_{method}"
            n = 2
            base = oid
            while oid in used:
                oid = f"{base}_{n}"
                n += 1
            used.add(oid)
            op["operationId"] = oid
            if "tags" not in op:
                op["tags"] = [tag]
            params = [
                x
                for x in op.get("parameters", [])
                if not (x.get("in") == "header" and x.get("name", "").lower() == "x-api-key")
            ]
            if params:
                op["parameters"] = params
            else:
                op.pop("parameters", None)

    comps = schema.setdefault("components", {})
    comps["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": "A session token from POST /api/v1/auth/login, or an API key that starts with bb_.",
        },
        "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key", "description": "An API key."},
    }
    schema["security"] = [{"bearerAuth": []}, {"apiKeyHeader": []}]
    app.openapi_schema = schema
    return schema


def install(app: FastAPI) -> None:
    app.openapi = lambda: build_openapi(app)  # type: ignore[method-assign]
