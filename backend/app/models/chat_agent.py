"""Chat agent (OpenPrintHQ farm feature): links between chat accounts and engine users.

See services/chat_agent/ and docs/chat-agent.md.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ChatIdentityLink(Base):
    """One chat account (a Slack or Discord user) acting as one engine user.

    Everything the bot does for this chat account runs with this engine user's
    permissions. A chat account links to at most one engine user; an engine user
    can have several chat accounts.
    """

    __tablename__ = "chat_identity_links"
    __table_args__ = (UniqueConstraint("platform", "external_user_id", name="uq_chat_link_platform_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))  # "slack" | "discord"
    external_user_id: Mapped[str] = mapped_column(String(64))
    # Slack workspace (team) id or Discord guild id the link was made from; informational.
    external_team_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChatLinkCode(Base):
    """A single-use code an engine user sends to the bot to link a chat account.

    Only a SHA-256 of the code is stored, so a database read does not reveal live codes.
    """

    __tablename__ = "chat_link_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
