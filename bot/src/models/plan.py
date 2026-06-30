from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    days: Mapped[int] = mapped_column(Integer)
    stars_price: Mapped[int] = mapped_column(Integer)
    # Price in RUB for the СБП/card path. NULL = no fiat price set yet, so that
    # method isn't offered for this plan.
    rub_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
