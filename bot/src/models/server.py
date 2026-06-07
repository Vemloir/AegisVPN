from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .server_access import ServerAccessGrant
    from .subscription import SubscriptionServer


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    flag: Mapped[str] = mapped_column(String(10))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    public_key: Mapped[str] = mapped_column(String(255))
    short_id: Mapped[str] = mapped_column(String(255))
    agent_url: Mapped[str] = mapped_column(String(255))
    agent_token: Mapped[str] = mapped_column(String(255))
    access_mode: Mapped[str] = mapped_column(String(32), default="public")
    subscription_group: Mapped[str] = mapped_column(String(16), default="safe")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    # A ready-made proxy URI served verbatim instead of fetching from an agent
    # (used for protocols our agents don't run, e.g. a standalone Hysteria2
    # node). `{uuid}`/`{host}` placeholders are substituted; the bot appends the
    # flagged label as the #fragment. NULL = normal agent-backed server.
    static_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mtproxy_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)

    subscription_servers: Mapped[list["SubscriptionServer"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    access_grants: Mapped[list["ServerAccessGrant"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
