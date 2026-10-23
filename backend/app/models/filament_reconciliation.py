"""Filament reconciliation: what the scale says versus what tracking expected.

See services/filament_reconciliation.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentReconciliation(Base):
    __tablename__ = "filament_reconciliations"

    id: Mapped[int] = mapped_column(primary_key=True)
    backend: Mapped[str] = mapped_column(String(10))  # local | spoolman
    spool_id: Mapped[int] = mapped_column(Integer, index=True)
    material: Mapped[str | None] = mapped_column(String(50), nullable=True)
    label_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Remaining filament the inventory held before the weighing, and what the
    # scale measured (gross minus the spool's empty weight).
    expected_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    measured_g: Mapped[float] = mapped_column(Float)
    # measured - expected: negative = more was used than tracked (unrecorded
    # prints, purge, failed prints); positive = less.
    drift_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
