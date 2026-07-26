from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow

if TYPE_CHECKING:
    from .subscription import Subscription


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    ua_fingerprint: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(100))
    # OS + version parsed from the User-Agent at creation (e.g. "iOS 17", "Windows 10/11").
    # Empty for clients that don't report it (Happ sends just "Android"/"Windows").
    os_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Client build number from the User-Agent (e.g. Happ's "Happ/2.9.1/Android/<build>").
    build_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Approximate location the device was added from (GeoIP of the IP that first
    # fetched the subscription), resolved once at creation. The IP itself is not stored.
    added_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(nullable=True, default=utcnow)
    # Server where traffic was last seen (set by poll_traffic)
    last_server_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("servers.id", ondelete="SET NULL"), nullable=True
    )
    traffic_up_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_down_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)

    subscription: Mapped["Subscription"] = relationship(back_populates="devices")
