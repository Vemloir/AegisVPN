from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

if TYPE_CHECKING:
    from .device import Device
    from .server import Server
    from .user import User


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sub_token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    legacy_sub_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    plan_days: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Cumulative traffic across all synced nodes, in bytes. Maintained by the
    # traffic poller (delta accounting); survives Xray restarts.
    traffic_up_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    servers: Mapped[list["SubscriptionServer"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    devices: Mapped[list["Device"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class SubscriptionServer(Base):
    __tablename__ = "subscription_servers"

    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    is_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last raw Xray counter seen on this node, for delta accounting. Xray
    # resets to 0 on restart; a drop below the stored value means restart.
    # Legacy single-email cursor — superseded by traffic_cursors below, kept
    # for backward compatibility with old rows.
    traffic_last_up: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_last_down: Mapped[int] = mapped_column(BigInteger, default=0)
    # Per-email raw cursor {email: [last_up, last_down]} for delta accounting.
    # A subscription now reports traffic under several emails (the base
    # user_X_sub_Y plus one user_X_sub_Y_dev_Z per device), so we track each
    # separately to survive Xray restarts and device add/remove.
    traffic_cursors: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Cumulative traffic on THIS node (per-location breakdown).
    traffic_up_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    subscription: Mapped["Subscription"] = relationship(back_populates="servers")
    server: Mapped["Server"] = relationship(back_populates="subscription_servers")
