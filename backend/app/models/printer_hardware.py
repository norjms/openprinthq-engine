"""Per-printer hardware configuration: nozzles and filament paths.

``printer_nozzles`` keeps one row per extruder. Rows with ``source='mqtt'``
mirror what the printer last reported; rows with ``source='manual'`` are user
overrides and always win. Extruder numbering is the DEVICE numbering: on Bambu
dual-nozzle printers 0 is the main (right) extruder and 1 the deputy (left);
on Klipper/Prusa it is the tool number T0..Tn.

``printer_filament_paths`` holds manual overrides of which extruders an AMS
slot (or a whole unit when ``tray_id`` is NULL) can feed.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class PrinterNozzle(Base):
    __tablename__ = "printer_nozzles"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), index=True)
    extruder_index: Mapped[int] = mapped_column(Integer)
    diameter_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    nozzle_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    volume_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    raw_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("printer_id", "extruder_index", name="uq_printer_nozzle_extruder"),)


class PrinterFilamentPath(Base):
    __tablename__ = "printer_filament_paths"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), index=True)
    ams_id: Mapped[int] = mapped_column(Integer)
    # NULL = the rule applies to every tray of the unit.
    tray_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extruders: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
