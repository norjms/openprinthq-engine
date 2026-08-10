"""Printer groups: named, user-defined sets of printers used as a queue target.

A queue item may target a group instead of a single printer. The scheduler then
assigns it to whichever member frees up first, reusing the same idle/connected/
filament checks as model-based assignment.

A printer may belong to several groups (a machine can be both "PLA farm" and
"Back room"), so membership is a plain many-to-many. This is deliberately
independent of ``Printer.location``, which stays as-is: location is where a
machine physically sits, a group is how you want to route work to it.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

printer_group_members = Table(
    "printer_group_members",
    Base.metadata,
    Column("group_id", Integer, ForeignKey("printer_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("printer_id", Integer, ForeignKey("printers.id", ondelete="CASCADE"), primary_key=True),
)


class PrinterGroup(Base):
    """A named set of printers that can be targeted as one queue destination."""

    __tablename__ = "printer_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    # Case-insensitive uniqueness guard, LOWER(TRIM(name)). Mirrors the pattern
    # used by locations so "PLA Farm" and "pla farm" cannot both exist.
    name_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Hex colour for the UI chip, e.g. "#4F8A6D". Cosmetic only.
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    printers: Mapped[list["Printer"]] = relationship(  # noqa: F821
        "Printer",
        secondary=printer_group_members,
        lazy="selectin",
    )


def group_name_key(name: str) -> str:
    """Canonical form used for case-insensitive uniqueness."""
    return (name or "").strip().lower()
