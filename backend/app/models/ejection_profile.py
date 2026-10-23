"""Bed-ejection profiles (OpenPrintHQ farm feature).

A profile is the G-code that pushes a finished part off the bed, plus the guards
that decide when it may run. A printer opts in with ``printers.auto_eject`` and
``printers.ejection_profile_id``; services/bed_ejection.py runs the profile after
a successful print and, only when every step worked, clears the plate-clear gate
so the queue can start the next job without a person at the printer.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class EjectionProfile(Base):
    __tablename__ = "ejection_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # The eject sequence, sent as one G-code script (Klipper macro name, or raw
    # G-code lines). Sent through the printer client's send_gcode.
    gcode: Mapped[str] = mapped_column(Text)
    # Wait until the bed has cooled to this temperature (C) before ejecting, so
    # the part releases from the plate. NULL = don't wait.
    max_bed_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Give up waiting for the bed to cool (or for the eject to finish) after this
    # many seconds; the gate stays raised and a person has to look.
    timeout_s: Mapped[int] = mapped_column(Integer, default=1800)
    # Seconds to wait after sending the sequence before checking the printer is
    # idle again (Bambu MQTT returns before the moves finish).
    settle_s: Mapped[int] = mapped_column(Integer, default=30)
    # After this many automatic ejects in a row the gate stays raised until a
    # person clears the plate (which resets the count). 0 = no limit.
    max_consecutive: Mapped[int] = mapped_column(Integer, default=5)
    # Only eject when the printer's installed plate is one of these (loose
    # names, compared like farm_match.normalize_plate). NULL/empty = any plate.
    allowed_plates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Check the bed with the camera plate detector after ejecting, and keep the
    # gate raised if anything is still on it. Needs plate detection calibrated.
    verify_with_camera: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
