from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .user import User


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Unique payment key. For Telegram Stars this is the telegram_payment_charge_id;
    # for Platega it is "platega_<transactionId>" — the uniqueness gives us callback
    # idempotency (a retried webhook maps to the same row).
    tg_payment_id: Mapped[str] = mapped_column(String(255), unique=True)
    stars_amount: Mapped[int] = mapped_column(Integer)
    plan_days: Mapped[int] = mapped_column(Integer)
    # "stars" (Telegram Stars) or "platega" (СБП/RUB).
    provider: Mapped[str] = mapped_column(String(16), default="stars")
    # Amount in rubles for Platega payments; NULL for Stars.
    rub_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lifecycle for Platega: "pending" at creation, "confirmed" once paid,
    # "canceled"/"chargeback" on failure. NULL for Stars (always settled).
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")
