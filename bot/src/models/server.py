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
    mtproxy_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Alternative VLESS+REALITY transport port on the SAME reality keypair
    # (public_key / short_id). NULL means the node serves xhttp/443 only and
    # offers no transport choice. Only the Greece node carries this today.
    tcp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Hysteria2 capability (a separate process; xray-core cannot speak it) ---
    # A node is Hy2-capable only when hy2_enabled AND an obfs password is set.
    # The obfs password is a secret, never shipped in the migration: the operator
    # sets it via a direct DB update, and emission falls back to vless until it is.
    hy2_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Client target port (the Hy2 server :listen port); the port-hop range start
    # in mport lets the client rotate UDP ports against ТСПУ throttling.
    hy2_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_hop_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_hop_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_obfs_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hy2_up: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hy2_down: Mapped[str | None] = mapped_column(String(32), nullable=True)

    @property
    def has_alt_transports(self) -> bool:
        """True when this node exposes an alternative VLESS transport (raw-tcp)
        beyond the default xhttp inbound, OR can serve Hysteria2 — i.e. a
        per-location protocol/transport choice is meaningful. Today only the
        Greece node qualifies on either count."""
        return self.tcp_port is not None or self.hy2_capable

    @property
    def hy2_capable(self) -> bool:
        """True only when the node can emit a USABLE Hy2 link: Hy2 is enabled,
        a client target port is set, and the obfs password (a secret left out of
        the migration) has been provisioned. A misconfigured node (enabled but
        no password) is NOT capable, so emission falls back to vless instead of
        shipping a broken Hy2 link."""
        return bool(self.hy2_enabled and self.hy2_port and self.hy2_obfs_password)

    subscription_servers: Mapped[list["SubscriptionServer"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    access_grants: Mapped[list["ServerAccessGrant"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
