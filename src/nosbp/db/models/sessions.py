"""Сессии веб-интерфейсов."""

import datetime
import uuid

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nosbp.core.constants import (
    ADMIN_SESSION_TOKEN_BYTES,
    INN_LENGTHS,
    IP_ADDRESS_MAX_LENGTH,
    SHA256_HEX_LENGTH,
)
from nosbp.db.base import Base, created_at_column, utcnow, uuid_pk

MAX_INN_LENGTH = max(INN_LENGTHS)
"""Колонка рассчитана на самый длинный вариант — ИНН физлица и ИП."""


class WebSession(Base):
    """Общая часть сессий панели управления и личного кабинета.

    Сессия хранится в базе, а не в подписанной куке: это позволяет
    отозвать её немедленно и не завершать сессии при перезапуске сервиса.
    Внешний ключ на владельца объявляется в наследнике.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)

    token_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_LENGTH), unique=True, index=True
    )
    """В базе только хэш — из дампа рабочую сессию не восстановить."""

    csrf_token: Mapped[str] = mapped_column(String(2 * ADMIN_SESSION_TOKEN_BYTES))
    """Токен форм: защищает от запросов, отправленных чужим сайтом."""

    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    ip_address: Mapped[str] = mapped_column(String(IP_ADDRESS_MAX_LENGTH), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = created_at_column()


class AdminSession(WebSession):
    """Сессия администратора панели управления."""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["owner_id"], ["admin_users.id"], ondelete="CASCADE"),
    )

    def __repr__(self) -> str:
        return f"<AdminSession owner={self.owner_id}>"


class AccountSession(WebSession):
    """Сессия заказчика в личном кабинете."""

    __tablename__ = "account_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
    )

    def __repr__(self) -> str:
        return f"<AccountSession owner={self.owner_id}>"
