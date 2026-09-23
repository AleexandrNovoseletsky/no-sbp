"""Учётные записи администраторов панели управления."""

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from nosbp.core.constants import (
    EMAIL_MAX_LENGTH,
    INN_LENGTHS,
    PASSWORD_HASH_MAX_LENGTH,
    TOTP_SECRET_LENGTH,
)
from nosbp.db.base import Base, created_at_column, uuid_pk

MAX_INN_LENGTH = max(INN_LENGTHS)
"""Колонка рассчитана на самый длинный вариант — ИНН физлица и ИП."""


class AdminUser(Base):
    """Администратор сервиса.

    Создаётся только консольной командой: регистрации через веб нет и
    быть не должно — панель управляет чужими деньгами.
    """

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), unique=True, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(PASSWORD_HASH_MAX_LENGTH))

    totp_secret: Mapped[str | None] = mapped_column(
        String(TOTP_SECRET_LENGTH), default=None
    )
    """Секрет для одноразовых кодов.

    Лежит открыто: тот, у кого есть доступ к базе, всё равно уже внутри
    периметра. Второй фактор защищает от увода пароля, а не от взлома
    сервера.
    """

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """До какого момента вход заблокирован после неудачных попыток."""

    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()

    def __repr__(self) -> str:
        return f"<AdminUser {self.email}>"
