from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class NodeSnapshot(Base):
    __tablename__ = "node_snapshots"

    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    page_size: Mapped[int] = mapped_column(Integer)


class NodeSnapshotPage(Base):
    __tablename__ = "node_snapshot_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["server_id", "generation"],
            ["node_snapshots.server_id", "node_snapshots.generation"],
            ondelete="CASCADE",
        ),
    )

    server_id: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    page_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_digest: Mapped[str] = mapped_column(String(64))
    items: Mapped[list[dict]] = mapped_column(JSON)


class NodeTelemetry(Base):
    __tablename__ = "node_telemetry"

    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
