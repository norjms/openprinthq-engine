"""Detected print failures, from every source, in one place (OpenPrintHQ farm feature).

See services/failure_detection.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrintFailureEvent(Base):
    __tablename__ = "print_failure_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(Integer, index=True)
    # bambu_hms | obico | klipper | stall
    source: Mapped[str] = mapped_column(String(20))
    code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # What happened because of it: notify | pause | pause_and_off | hold
    action_taken: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
