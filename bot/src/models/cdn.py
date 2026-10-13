from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class CdnEndpoint(Base):
    """One CDN resource that proxies to a node's CDN origin inbound.

    Mobile allowlists admit a CDN's edge addresses while dropping the node's
    own, so a client that connects to the edge still reaches the node. The
    resource itself is created by hand in the provider's panel (origin =
    ``Server.cdn_origin_host:Server.cdn_port``); this row tells clients how to
    reach it. A node may sit behind several resources, at several providers,
    so one being banned or broken never takes the path down.
    """

    __tablename__ = "cdn_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        index=True,
    )
    # Operator-facing provider name ("Beget", "Timeweb"); never shown to users.
    provider: Mapped[str] = mapped_column(String(64))
    # The resource's hostname: the HTTP Host every request carries, and the TLS
    # SNI / connect address unless overridden below.
    host: Mapped[str] = mapped_column(String(255))
    # Optional connect address (an edge IP) when it must differ from ``host``.
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional TLS SNI when it must differ from ``host`` (domain fronting).
    sni: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional xhttp ``extra`` for what a particular CDN tolerates (e.g. Yandex
    # rejects request bodies, so uplink must ride in GET headers there).
    xhttp_extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Trust the edge's certificate by its hash instead of by the CA chain. For
    # fronting through a name whose certificate the edge serves but that would
    # not verify (e.g. expired). The reachability check learns the hashes into
    # ``pinned_cert_sha256`` (comma-separated hex, newest first); until it has
    # one the resource is not advertised.
    pin_cert: Mapped[bool] = mapped_column(Boolean, default=False)
    pinned_cert_sha256: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Only shown as a "| WL" preview entry, never in anyone's fallback tier.
    preview_only: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    # Result of the periodic reachability check (scheduler.check_cdn_endpoints).
    # NULL = never checked; a resource is advertised until it is known broken.
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_check_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def connect_address(self) -> str:
        return (self.address or "").strip() or self.host

    @property
    def tls_server_name(self) -> str:
        return (self.sni or "").strip() or self.host

    @property
    def cert_pins(self) -> list[str]:
        return [pin for pin in (self.pinned_cert_sha256 or "").split(",") if pin]
