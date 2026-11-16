"""Saved filament mappings: "for this requested filament, this loaded one is fine" (filament matching).

See services/filament_match.py. A mapping someone chose with "remember" counts as an exact match from then on,
on every printer.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentAlias(Base):
    __tablename__ = "filament_aliases"
    __table_args__ = (UniqueConstraint("req_type", "req_color", "tray_type", "tray_color", name="uq_filament_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    req_type: Mapped[str] = mapped_column(String(50))
    req_color: Mapped[str] = mapped_column(String(9))
    tray_type: Mapped[str] = mapped_column(String(50))
    tray_color: Mapped[str] = mapped_column(String(9))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
