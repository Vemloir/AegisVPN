from sqlalchemy import Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    days: Mapped[int] = mapped_column(Integer)
    stars_price: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
