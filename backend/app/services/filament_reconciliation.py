"""Filament reconciliation (OpenPrintHQ farm feature).

Usage tracking estimates what each print takes off a spool. A scale reading
(SpoolBuddy) is the truth. Each weighing now records both numbers before the
inventory is overwritten, so drift shows up per spool, per material and over
time instead of vanishing: ``GET /api/v1/inventory/reconciliation``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.app.models.filament_reconciliation import FilamentReconciliation


def record(
    db,
    *,
    backend: str,
    spool_id: int,
    material: str | None,
    label_g: float | None,
    expected_g: float | None,
    measured_g: float,
) -> FilamentReconciliation:
    """Add a reconciliation row to the session (the caller commits)."""
    drift = None if expected_g is None else round(measured_g - expected_g, 1)
    row = FilamentReconciliation(
        backend=backend,
        spool_id=spool_id,
        material=(material or None) and str(material)[:50],
        label_g=label_g,
        expected_g=None if expected_g is None else round(expected_g, 1),
        measured_g=round(measured_g, 1),
        drift_g=drift,
    )
    db.add(row)
    return row


async def report(db, days: int = 90, flag_pct: float = 5.0) -> dict:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = (
        (
            await db.execute(
                select(FilamentReconciliation)
                .where(FilamentReconciliation.created_at >= since)
                .order_by(FilamentReconciliation.id.desc())
            )
        )
        .scalars()
        .all()
    )
    by_material: dict[str, dict] = {}
    flagged = []
    for r in rows:
        if r.drift_g is None:
            continue
        m = by_material.setdefault(r.material or "unknown", {"weighings": 0, "total_drift_g": 0.0})
        m["weighings"] += 1
        m["total_drift_g"] = round(m["total_drift_g"] + r.drift_g, 1)
        if r.label_g and abs(r.drift_g) > r.label_g * flag_pct / 100.0:
            flagged.append(r.id)
    for m in by_material.values():
        m["avg_drift_g"] = round(m["total_drift_g"] / m["weighings"], 1) if m["weighings"] else None
    return {
        "days": days,
        "flag_pct": flag_pct,
        "weighings": len(rows),
        "by_material": by_material,
        "flagged_ids": flagged,
        "rows": [
            {
                "id": r.id,
                "backend": r.backend,
                "spool_id": r.spool_id,
                "material": r.material,
                "label_g": r.label_g,
                "expected_g": r.expected_g,
                "measured_g": r.measured_g,
                "drift_g": r.drift_g,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows[:500]
        ],
    }
