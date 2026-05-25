from sqlalchemy import BigInteger, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)  # tg_id of referrer
    referred_id: Mapped[int] = mapped_column(BigInteger, index=True, unique=True)  # tg_id of referred user
    bonus_days_given: Mapped[int] = mapped_column(Integer, default=0)
