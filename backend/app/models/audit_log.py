"""Audit log of changes made through the API (OpenPrintHQ farm feature).

See services/audit.py.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # Who: a signed-in user, an API key, or (behind the OpenPrintHQ control plane,
    # where the engine runs without its own sign-in) the actor the control plane
    # names in X-OPHQ-Actor. actor_kind says which.
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20), default="anonymous")  # user | api_key | gateway | anonymous
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method: Mapped[str] = mapped_column(String(10))
    # The route template (/api/v1/printers/{printer_id}) and the concrete path.
    route: Mapped[str | None] = mapped_column(String(300), nullable=True)
    path: Mapped[str] = mapped_column(String(500))
    status: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
