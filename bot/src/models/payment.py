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
    tg_payment_id: Mapped[str] = mapped_column(String(255), unique=True)
    stars_amount: Mapped[int] = mapped_column(Integer)
    plan_days: Mapped[int] = mapped_column(Integer)

    user: Mapped["User"] = relationship(back_populates="payments")
