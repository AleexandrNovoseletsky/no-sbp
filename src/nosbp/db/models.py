"""Модели базы данных."""

import uuid
import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Client(Base):
    """Клиент сервиса — компания со своими реквизитами для приёма платежей."""

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Ключ, который клиент передаёт в запросе (?api_key=...)
    api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Просто для вашего удобства в БД — не участвует в логике
    display_name: Mapped[str] = mapped_column(String(160))

    # ==== Реквизиты получателя платежа (статичные, из PaymentDetails) ====
    name: Mapped[str] = mapped_column(String(160))
    personal_acc: Mapped[str] = mapped_column(String(20))
    bank_name: Mapped[str] = mapped_column(String(45))
    bic: Mapped[str] = mapped_column(String(9))
    corresp_acc: Mapped[str] = mapped_column(String(20))
    payee_inn: Mapped[str] = mapped_column(String(12))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc)
        )
