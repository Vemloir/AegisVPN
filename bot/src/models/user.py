from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .payment import Payment
    from .server_access import ServerAccessGrant
    from .subscription import Subscription


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(8), default="ru", server_default="ru")
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    privacy_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Legal-acceptance gate. Acceptance is valid only while
    # accepted_terms_version == core.terms.TERMS_VERSION; a version bump
    # re-prompts the user. accepted_terms_at records when they last accepted.
    accepted_terms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_terms_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-user simultaneous-connection override. NULL = node default applies;
    # 0 = unlimited; N>0 = at most N concurrent source IPs.
    conn_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    server_access_grants: Mapped[list["ServerAccessGrant"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
