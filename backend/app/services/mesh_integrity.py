"""Mesh integrity analysis for library files.

Answers one question about a model file: would a slicer choke on it, or is a
failed print more likely to be the printer's fault? A non-manifold mesh or one
with holes is the most common cause of a failure that presents as a machine
problem, so the signal is worth surfacing next to the file rather than leaving
the operator to discover it in the slicer.

Deliberate choices, recorded because each one has a cheaper wrong answer:

* **trimesh**, because it is already a dependency (``requirements.txt``) used by
  the STL and plate thumbnailers, and it is already proven on the aarch64 image
  that production runs. Nothing new has to build a wheel.
* **No scipy.** ``Trimesh.outline()`` needs it and it is not installed, so
  boundary loops are counted with a small union-find over the boundary edges
  instead of pulling a large numerical dependency into every tenant image.
* **Vertices are merged before analysis** (trimesh's default processing). An STL
  is a bag of unconnected triangles on disk, so without the merge every single
  edge is a boundary edge and every file looks catastrophically broken. This is
  also what a slicer does, so it is the honest comparison.
* **Not everything detectable is worth reporting.** Degenerate faces and unusual
  scale are recorded in the stats block but never raise a problem, because they
  are common in files that print perfectly and a badge everyone learns to ignore
  is worse than no badge.

A file that cannot be parsed is NOT a file with a problem. The status vocabulary
keeps those apart so a STEP file never becomes a false positive.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bumped whenever the analysis changes in a way that makes a cached result
# wrong. Cached rows carrying an older version are recomputed on the next pass.
ANALYZER_VERSION = 1

# Extensions trimesh can turn into a triangle mesh. Everything else the library
# scanner tracks (STEP, G-code, images) resolves to "unsupported" before trimesh
# is ever called, so a boundary-representation CAD file can never be reported as
# unreadable.
SUPPORTED_EXTENSIONS = {".stl", ".obj", ".ply", ".off", ".3mf"}

# Statuses. Only STATUS_PROBLEMS should ever produce a badge in the UI.
STATUS_OK = "ok"
STATUS_PROBLEMS = "problems"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNREADABLE = "unreadable"
STATUS_TOO_LARGE = "too_large"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# Ceilings. Above these the file is skipped as too_large rather than analysed,
# because this is shared infrastructure and one 2GB scan should not evict every
# other tenant's working set. Both are generous: a 256MiB STL is roughly five
# million triangles.
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_FACES = 3_000_000

# Below this a file cannot contain a single binary-STL triangle, so there is
# nothing to analyse and nothing worth logging about.
MIN_USABLE_BYTES = 134

_HASH_CHUNK = 1024 * 1024


def content_hash(path: Path) -> str:
    """SHA256 of a file's bytes, read in chunks.

    The cache key. Chunked because the library holds files far larger than it is
    reasonable to hold in memory, and the external scanner deliberately skips
    hashing for speed, so this function is the only place the hash gets paid for.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:  # SEC-PATH-OK: caller resolves within the library root
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def is_supported(filename: str) -> bool:
    """Can this filename plausibly be turned into a mesh?"""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return False
    # A sliced ".gcode.3mf" is a G-code container that happens to be a 3MF zip.
    # Loading it as geometry is meaningless, so exclude the compound suffix.
    suffixes = [s.lower() for s in Path(filename).suffixes]
    return not (len(suffixes) >= 2 and suffixes[-2:] == [".gcode", ".3mf"])


def _boundary_loop_count(boundary_edges) -> int:
    """Count connected components among boundary edges: one per hole.

    Union-find rather than a graph library. The boundary edge set is small even
    on a badly broken mesh, and this keeps the analysis free of scipy and
    networkx.
    """
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in boundary_edges:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return len({find(v) for v in parent})


def _analyze_mesh(mesh) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Findings plus stats for a loaded mesh. Pure, so it is directly testable."""
    import numpy as np

    findings: list[dict[str, Any]] = []

    # Each undirected edge should be shared by exactly two faces. Once is a
    # boundary (a hole), more than twice is non-manifold. np.unique rather than
    # trimesh.grouping.group_rows: same answer, no ragged python list to walk.
    edges = mesh.edges_sorted
    if len(edges):
        uniq, counts = np.unique(edges, axis=0, return_counts=True)
    else:
        uniq, counts = np.empty((0, 2), dtype=int), np.empty(0, dtype=int)

    boundary_mask = counts == 1
    boundary_count = int(boundary_mask.sum())
    non_manifold_count = int((counts > 2).sum())

    hole_count = _boundary_loop_count(uniq[boundary_mask]) if boundary_count else 0

    if boundary_count:
        findings.append(
            {
                "code": "holes",
                "severity": SEVERITY_ERROR,
                "count": hole_count,
                "detail": f"{hole_count} hole(s), {boundary_count} open edge(s)",
            }
        )
    if non_manifold_count:
        findings.append(
            {
                "code": "non_manifold_edges",
                "severity": SEVERITY_ERROR,
                "count": non_manifold_count,
                "detail": f"{non_manifold_count} edge(s) shared by more than two faces",
            }
        )

    # Winding is a warning, not an error: most slicers recover from flipped
    # normals, but it is worth knowing when a print comes out inside-out.
    winding_ok = True
    try:
        winding_ok = bool(mesh.is_winding_consistent)
    except Exception as exc:  # noqa: BLE001 - a property that raises is not a defect
        logger.debug("winding check unavailable: %s", exc)
    if not winding_ok:
        findings.append(
            {
                "code": "inconsistent_winding",
                "severity": SEVERITY_WARNING,
                "count": 1,
                "detail": "face normals do not agree on which side is outside",
            }
        )

    # Recorded, never badged. Degenerate faces are ubiquitous in exports that
    # print fine, and scale is a judgement about intent, not about the mesh.
    areas = mesh.area_faces
    scale = float(mesh.scale) if mesh.scale else 1.0
    degenerate = int((areas <= (scale**2) * 1e-12).sum()) if len(areas) else 0
    extents = [float(x) for x in mesh.extents] if mesh.extents is not None else [0.0, 0.0, 0.0]
    largest = max(extents) if extents else 0.0

    stats = {
        "faces": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "boundary_edges": boundary_count,
        "holes": hole_count,
        "non_manifold_edges": non_manifold_count,
        "degenerate_faces": degenerate,
        "winding_consistent": winding_ok,
        "watertight": bool(mesh.is_watertight),
        "extents_mm": [round(x, 3) for x in extents],
        "suspect_scale": bool(largest and (largest < 1.0 or largest > 1000.0)),
    }
    return findings, stats


def analyze_file(path: Path, max_file_bytes: int = MAX_FILE_BYTES, max_faces: int = MAX_FACES) -> dict[str, Any]:
    """Analyse one file. Never raises: every outcome is a status.

    Blocking and CPU-bound. Callers on the event loop should hand it to
    ``asyncio.to_thread``.
    """
    started = time.perf_counter()

    def result(status: str, *, findings=None, stats=None, reason=None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": status,
            "analyzer_version": ANALYZER_VERSION,
            "findings": findings or [],
            "stats": stats or {},
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        if reason:
            out["reason"] = reason
        return out

    if not is_supported(path.name):
        return result(STATUS_UNSUPPORTED, reason=f"{path.suffix.lower() or 'no extension'} is not a mesh format")

    try:
        size = path.stat().st_size
    except OSError as exc:
        return result(STATUS_UNREADABLE, reason=f"could not stat the file: {exc}")

    if size < MIN_USABLE_BYTES:
        return result(STATUS_UNREADABLE, reason="file is too small to contain a mesh")
    if size > max_file_bytes:
        return result(STATUS_TOO_LARGE, reason=f"{size} bytes is over the {max_file_bytes} byte analysis ceiling")

    try:
        import trimesh

        # Default processing merges duplicate vertices, which is required: see
        # the module docstring. force="mesh" collapses a multi-body scene into
        # one mesh so a 3MF with several objects is analysed as a whole.
        mesh = trimesh.load(str(path), force="mesh")
    except Exception as exc:  # noqa: BLE001 - an unparseable file is not a broken one
        return result(STATUS_UNREADABLE, reason=f"could not be parsed as a mesh: {exc}")

    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        return result(STATUS_UNREADABLE, reason="parsed but contains no triangles")

    if len(mesh.faces) > max_faces:
        return result(
            STATUS_TOO_LARGE,
            stats={"faces": int(len(mesh.faces))},
            reason=f"{len(mesh.faces)} faces is over the {max_faces} face analysis ceiling",
        )

    try:
        findings, stats = _analyze_mesh(mesh)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mesh analysis failed for %s: %s", path, exc)
        return result(STATUS_UNREADABLE, reason=f"analysis failed: {exc}")

    return result(STATUS_PROBLEMS if findings else STATUS_OK, findings=findings, stats=stats)


# ============ Cached pass over the library ============
#
# Analysis is deliberately NOT part of the folder scan. A scan runs on every
# upload for every tenant on shared infrastructure, and folding a per-file mesh
# load into it would change what a scan costs for everyone at once. Instead the
# scan spawns this pass afterwards, the same shape as the STL thumbnail
# backfill, and the pass is a no-op unless the tenant has turned it on.

SETTING_ENABLED = "library_mesh_integrity_enabled"

# One pass analyses at most this many files. A library with ten thousand new
# models gets worked through over several scans rather than pinning a core for
# an hour on the first one.
DEFAULT_PASS_LIMIT = 200


async def integrity_enabled(db) -> bool:
    """Is mesh integrity checking switched on? Off unless explicitly enabled."""
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, SETTING_ENABLED)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def analyze_library_file(db, lib_file, *, force: bool = False) -> Any:
    """Analyse one ``LibraryFile`` and upsert its cached report.

    Returns the report row, or ``None`` when the file is not on disk. Reuses a
    cached row whose hash and analyzer version both match, so a rescan of an
    unchanged library costs one hash per file and nothing else.
    """
    import asyncio

    from sqlalchemy import select

    # Lazy: the resolver lives in the routes module, which imports this one.
    from backend.app.api.routes.library import to_absolute_path
    from backend.app.models.library import LibraryMeshReport

    abs_path = to_absolute_path(lib_file.file_path)
    if not abs_path or not abs_path.exists():
        return None

    file_hash = lib_file.file_hash
    if not file_hash or force:
        try:
            file_hash = await asyncio.to_thread(content_hash, abs_path)
        except OSError as exc:
            logger.debug("mesh integrity could not hash %s: %s", abs_path, exc)
            return None
        # The external scanner skips hashing for speed, so backfill it here.
        # Duplicate detection gets the same value for free.
        lib_file.file_hash = file_hash

    existing = (
        await db.execute(select(LibraryMeshReport).where(LibraryMeshReport.file_hash == file_hash))
    ).scalar_one_or_none()
    if existing is not None and existing.analyzer_version == ANALYZER_VERSION and not force:
        return existing

    # Off the event loop: trimesh.load on a large STL is seconds of CPU and this
    # process also serves printer telemetry.
    result = await asyncio.to_thread(analyze_file, abs_path)

    row = existing or LibraryMeshReport(file_hash=file_hash)
    row.analyzer_version = result["analyzer_version"]
    row.status = result["status"]
    row.findings = result["findings"]
    row.stats = result["stats"]
    row.reason = result.get("reason")
    row.duration_ms = result["duration_ms"]
    try:
        row.file_size = abs_path.stat().st_size
    except OSError:
        row.file_size = None
    if existing is None:
        db.add(row)
    return row


async def run_pass(folder_ids: list[int] | None = None, limit: int = DEFAULT_PASS_LIMIT, force: bool = False) -> dict:
    """Analyse library files that have no current report.

    Opens its own session: when spawned after a scan, the request session is
    already closed. Commits per file so a restart mid-pass only loses the file
    in flight.
    """
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile, LibraryMeshReport

    analysed = 0
    skipped = 0
    async with async_session() as db:
        if not force and not await integrity_enabled(db):
            return {"analysed": 0, "skipped": 0, "reason": "disabled"}

        stmt = LibraryFile.active()
        if folder_ids:
            stmt = stmt.where(LibraryFile.folder_id.in_(folder_ids))
        files = (await db.execute(stmt)).scalars().all()

        # Cheap filter first: never open a STEP file, a G-code file or an image.
        candidates = [f for f in files if is_supported(f.filename)]

        # Files whose hash already has a current report need no work at all.
        known: set[str] = set()
        hashes = [f.file_hash for f in candidates if f.file_hash]
        if hashes and not force:
            rows = (
                (
                    await db.execute(
                        select(LibraryMeshReport.file_hash).where(
                            LibraryMeshReport.file_hash.in_(hashes),
                            LibraryMeshReport.analyzer_version == ANALYZER_VERSION,
                        )
                    )
                )
                .scalars()
                .all()
            )
            known = set(rows)

        for lib_file in candidates:
            if analysed >= limit:
                break
            if not force and lib_file.file_hash and lib_file.file_hash in known:
                skipped += 1
                continue
            try:
                row = await analyze_library_file(db, lib_file, force=force)
            except Exception as exc:  # noqa: BLE001 - one bad file must not end the pass
                logger.debug("mesh integrity pass skipped %s: %s", lib_file.file_path, exc)
                await db.rollback()
                continue
            if row is None:
                skipped += 1
                continue
            analysed += 1
            await db.commit()

    if analysed:
        logger.info("Mesh integrity pass: analysed %d file(s), skipped %d", analysed, skipped)
    return {"analysed": analysed, "skipped": skipped}
