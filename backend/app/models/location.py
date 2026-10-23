from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.utils.location_codes import generate_location_code

if TYPE_CHECKING:
    from backend.app.models.spool import Spool


class Location(Base):
    """Physical storage location for filament spools (shelf, drawer, drybox, etc.)."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Case-insensitive uniqueness — LOWER(TRIM(name)); enforced via migration index.
    name_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Free-form external identifier (e.g. an RFID shelf tag).
    identifier: Mapped[str | None] = mapped_column(String(100))
    # What kind of place this is: storage | drybox | printer | ams | ams_slot | external | other.
    kind: Mapped[str] = mapped_column(String(20), default="storage", server_default="storage")
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Printer hardware binding for printer/ams/ams_slot/external locations.
    printer_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ams_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tray_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stable scan code printed on labels ("L" + 6 Crockford base32 chars).
    code: Mapped[str | None] = mapped_column(
        String(16), nullable=True, unique=True, index=True, default=generate_location_code
    )
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    spools: Mapped[list["Spool"]] = relationship(back_populates="location")
