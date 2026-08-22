from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, Integer, String
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
    # Node-initiated control plane. Existing nodes remain on "push" until a
    # canary has observed and then applied an exact desired-state snapshot.
    control_mode: Mapped[str] = mapped_column(String(16), default="push")
    control_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    control_cert_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    # Rotation overlap accepts either complete credential pair for a bounded
    # window. Certificate/token halves are never mix-and-matched.
    control_previous_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    control_previous_cert_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    control_previous_credential_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    desired_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_generation: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    control_last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    control_last_reconciled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    control_last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    control_agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    control_capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Cascade routing is dormant by default. Existing nodes migrate to "both"
    # so direct locations remain byte-for-byte unchanged; a future Russian
    # entry is explicitly enrolled as "entry".
    node_role: Mapped[str] = mapped_column(String(16), default="both")
    # Website presentation only — never used to route or connect. ISO 3166-1
    # alpha-2 ("FI"). The site derives the globe outline, the camera target and
    # the region filter from it; a node without it is served by the bot but not
    # drawn on the globe.
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)

    access_mode: Mapped[str] = mapped_column(String(32), default="public")
    subscription_group: Mapped[str] = mapped_column(String(16), default="safe")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Delist from the rendered subscription (vless list / xray-JSON) without
    # touching access control or the control-plane desired-state: existing
    # clients already synced to this node keep connecting on their saved
    # config, only the subscription output stops advertising it as an option.
    # Distinct from is_active/access_mode, which both flow through
    # ServerAccessService reconcile and DO strip the node's live client list.
    hidden_from_subscription: Mapped[bool] = mapped_column(Boolean, default=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    mtproxy_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # MTProto-proxy (fake-TLS mtg) listen port. Together with mtproxy_secret it
    # makes the node MTProxy-capable; both are operator-set (provisioned by
    # deploy/vps/update.py --mtproxy), never shipped in the migration.
    mtproxy_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Alternative VLESS+REALITY transport port on the SAME reality keypair
    # (public_key / short_id). NULL means the node serves xhttp/443 only and
    # offers no transport choice.
    tcp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Lifetime traffic through this location, accumulated by the poll_traffic task
    # alongside the per-subscription counters. It lives on the Server row (not only
    # on SubscriptionServer links) precisely so a location's history survives the
    # link churn that disabling/re-syncing causes — the link rows are deleted on
    # reconcile, this total is not.
    traffic_up_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    # Snapshot of the last periodic health check's online-client count
    # (check_servers_health, bot/src/scheduler/tasks.py). Used to keep
    # over-loaded nodes out of the "Автовыбор" balancer candidate set without
    # an extra live call at subscription-render time.
    last_seen_online_clients: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Hysteria2 capability (a separate process; xray-core cannot speak it) ---
    # A node is Hy2-capable only when hy2_enabled AND an obfs password is set.
    # The obfs password is a secret, never shipped in the migration: the operator
    # sets it via a direct DB update, and emission falls back to vless until it is.
    hy2_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Client target port (the Hy2 server :listen port); the port-hop range start
    # in mport lets the client rotate UDP ports when a network throttles a
    # long-lived flow on a single port.
    hy2_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_hop_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_hop_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hy2_obfs_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Optional Xray-fork client congestion controller. NULL preserves the
    # client's native default; only the safe no-bandwidth modes are emitted.
    # This is deliberately independent of Salamander so a plain-QUIC node can
    # select Reno without carrying an obfuscation password.
    hy2_congestion: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Optional ClientHello SNI used only by Xray-JSON clients. The certificate
    # is still verified against ``hy2_sni`` via verifyPeerCertByName, allowing a
    # filtered path to use a known-good large-provider SNI without disabling TLS
    # verification or changing the node certificate.
    hy2_camouflage_sni: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hy2_up: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hy2_down: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Hy2 TLS SNI for this node's own certificate. Nodes sharing an SNI still
    # require separate per-node bundles; the control endpoint rejects a shared
    # long-lived private key. It is required for a usable client link.
    hy2_sni: Mapped[str | None] = mapped_column(String(255), nullable=True)

    @property
    def has_alt_transports(self) -> bool:
        """Every location exposes the per-location settings screen (Protocol:
        VLESS + Transport: the inbound it serves), so all locations are configured
        identically — no node is the odd one out showing "standard transport only".
        Hy2 is gated off in the bot, so it no longer drives this; a node with a
        tcp_port additionally offers a real transport choice in that screen."""
        return True

    @property
    def hy2_capable(self) -> bool:
        """True only when the node can emit a USABLE Hy2 link: Hy2 is enabled,
        a client target port is set, and the CA cert SNI (operator-provisioned)
        is present. The obfs password is NO LONGER required — Hy2 now listens on
        UDP 443 with no obfuscation (looks like QUIC). A misconfigured node
        (enabled but missing port or SNI) is NOT capable, so emission falls back
        to vless instead of shipping a broken Hy2 link."""
        return bool(self.hy2_enabled and self.hy2_port and self.hy2_sni)

    @property
    def mtproxy_capable(self) -> bool:
        """True when the node can hand out an MTProto-proxy link: both the mtg
        fake-TLS secret and the listen port are provisioned (operator-set, never
        in the migration). Falls back to no proxy link until both are present."""
        return bool(self.mtproxy_secret and self.mtproxy_port)

    subscription_servers: Mapped[list["SubscriptionServer"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
    access_grants: Mapped[list["ServerAccessGrant"]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )
