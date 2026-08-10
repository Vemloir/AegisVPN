from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .server import Server
    from .user import User


class ServerTransportPref(Base):
    """A user's per-location protocol/transport choice.

    Keyed by ``(user_id, server_id)`` — NOT by subscription — so the preference
    survives a subscription reissue (which mints a new ``subscription_id`` but
    keeps the same user). A MISSING row means the current capability-aware
    VLESS default (TCP when available, otherwise XHTTP); we only store a row
    when the user picks something
    other than the server's capability-aware default, and ``reset`` deletes the
    row to return to that default.
    """

    __tablename__ = "server_transport_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    # "vless" (default) | "hy2" (future; never emitted until a Hy2 backend exists).
    protocol: Mapped[str] = mapped_column(String(16), default="vless")
    # "tcp" (product default) | "xhttp". Only meaningful for protocol == "vless".
    transport: Mapped[str] = mapped_column(String(16), default="tcp")

    user: Mapped["User"] = relationship()
    server: Mapped["Server"] = relationship()
