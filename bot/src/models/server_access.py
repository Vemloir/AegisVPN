from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .server import Server
    from .user import User


class ServerAccessGrant(Base):
    __tablename__ = "server_access_grants"

    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)

    server: Mapped["Server"] = relationship(back_populates="access_grants")
    user: Mapped["User"] = relationship(back_populates="server_access_grants")
