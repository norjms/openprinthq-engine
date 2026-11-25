"""Printer alarms: every active problem a printer reports, in one feed (OpenPrintHQ farm feature).

See services/alarm_feed.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterAlarm(Base):
    __tablename__ = "printer_alarms"
    __table_args__ = (Index("ix_printer_alarms_printer_open", "printer_id", "cleared_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(Integer, index=True)
    # Which transport reported it: bambu | klipper | octoprint | prusalink | duet |
    # flashforge | mks | snapmaker | elegoo | elegoo_cc2 |
    # anycubic
    source: Mapped[str] = mapped_column(String(20))
    # Bambu HMS short code (0700_8010), or a transport code (printer_error,
    # attention, klippy_shutdown, ...)
    code: Mapped[str] = mapped_column(String(40))
    # fatal | serious | warning
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Set when the printer stopped reporting it. NULL = active.
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Bambu's full HMS code (16 hex characters) when the source is bambu.
    full_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Set when someone dismissed it: it leaves the active list while the printer
    # keeps reporting it, and comes back as a new alarm if it clears and recurs.
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AlarmSuppression(Base):
    """An alarm code someone chose not to be alerted about on one printer.

    The feed records nothing for a suppressed code on that printer, and Bambu HMS
    notifications skip it, until the suppression is removed.
    """

    __tablename__ = "alarm_suppressions"
    __table_args__ = (UniqueConstraint("printer_id", "source", "code", name="uq_alarm_suppression"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(40))
    # The alarm's text when it was suppressed, so the list says what it was.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
