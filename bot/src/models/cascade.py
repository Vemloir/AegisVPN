from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class CascadeRoute(Base):
    __tablename__ = "cascade_routes"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(255))
    entry_server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    health_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    transport_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CascadeRouteExit(Base):
    __tablename__ = "cascade_route_exits"
    __table_args__ = (
        UniqueConstraint("service_uuid", name="uq_cascade_service_uuid"),
        UniqueConstraint("route_id", "position", name="uq_cascade_exit_position"),
    )

    route_id: Mapped[int] = mapped_column(
        ForeignKey("cascade_routes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    exit_server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    service_uuid: Mapped[str] = mapped_column(String(36))
    server_name: Mapped[str] = mapped_column(String(255))
    xhttp_path: Mapped[str] = mapped_column(String(255), default="/")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class CascadeRouteAck(Base):
    __tablename__ = "cascade_route_acks"

    route_id: Mapped[int] = mapped_column(
        ForeignKey("cascade_routes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    config_digest: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    acknowledged_at: Mapped[datetime] = mapped_column(default=utcnow)
