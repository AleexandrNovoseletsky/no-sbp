"""Регистрация и вход в личный кабинет заказчика."""

import datetime
import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.errors import (
    AccountDisabledError,
    CabinetAuthError,
    CabinetLockedError,
    ValidationError,
)
from nosbp.core.lockout import ensure_not_locked, register_failure, reset_failures
from nosbp.core.security import (
    hash_password,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from nosbp.core.sessions import IssuedSession, SessionStore
from nosbp.db.base import utcnow
from nosbp.db.models import Account, AccountSession

SESSION_COOKIE_NAME: Final[str] = "nosbp_cabinet"

GENERIC_AUTH_MESSAGE: Final[str] = "Неверная почта или пароль."
"""Единый текст для всех неудач входа.

Различие сообщений позволило бы установить, какие адреса зарегистрированы.
"""

_DUMMY_HASH: Final[str] = hash_password("проверка времени ответа, не пароль")


class CabinetAuthService:
    """Регистрация, вход и сессии заказчиков."""

    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self.sessions: SessionStore[AccountSession] = SessionStore(
            db,
            AccountSession,
            ttl_hours=settings.cabinet_session_ttl_hours,
            idle_minutes=settings.cabinet_session_idle_minutes,
        )

    # ------------------------------------------------------------------
    # Регистрация
    # ------------------------------------------------------------------

    async def register(
        self, *, email: str, password: str, display_name: str
    ) -> Account:
        """Создаёт учётную запись заказчика.

        :raises ValidationError: адрес занят, пароль слаб или поля пусты.
        """
        if not self._settings.cabinet_registration_open:
            raise ValidationError("Регистрация временно закрыта. Напишите в поддержку.")

        normalized = email.strip().lower()
        if not normalized:
            raise ValidationError("Укажите адрес почты.")
        if not display_name.strip():
            raise ValidationError("Укажите название компании.")

        complaint = validate_password_strength(password)
        if complaint is not None:
            raise ValidationError(complaint)

        if await self._email_taken(normalized):
            raise ValidationError(
                "Этот адрес уже зарегистрирован. Войдите или восстановите пароль."
            )

        account = Account(
            email=normalized,
            display_name=display_name.strip(),
            password_hash=hash_password(password),
            daily_charge_limit_kopecks=(
                self._settings.default_daily_charge_limit_kopecks
            ),
        )
        self._db.add(account)
        await self._db.flush()
        return account

    async def _email_taken(self, email: str) -> bool:
        """Проверяет, занят ли адрес почты."""
        result = await self._db.execute(
            select(func.count()).select_from(Account).where(Account.email == email)
        )
        return bool(result.scalar_one())

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

    async def authenticate(self, *, email: str, password: str) -> Account:
        """Проверяет пару «почта — пароль».

        :raises CabinetLockedError: вход заблокирован после неудачных попыток.
        :raises AccountDisabledError: учётная запись отключена оператором.
        :raises CabinetAuthError: неверные учётные данные.
        """
        now = utcnow()
        account = await self._find(email)

        if account is None or account.password_hash is None:
            # Пароль всё равно проверяется, чтобы время ответа не выдавало,
            # зарегистрирован такой адрес или нет.
            verify_password(_DUMMY_HASH, password)
            raise CabinetAuthError(GENERIC_AUTH_MESSAGE)

        ensure_not_locked(account, now, CabinetLockedError)

        if not verify_password(account.password_hash, password):
            await self._register_failure(account, now)
            raise CabinetAuthError(GENERIC_AUTH_MESSAGE)

        if not account.is_active:
            raise AccountDisabledError(
                "Учётная запись отключена. Напишите в поддержку."
            )

        if password_needs_rehash(account.password_hash):
            account.password_hash = hash_password(password)

        reset_failures(account)
        account.last_login_at = now
        return account

    async def _find(self, email: str) -> Account | None:
        """Находит заказчика по адресу почты."""
        result = await self._db.execute(
            select(Account).where(Account.email == email.strip().lower())
        )
        return result.scalar_one_or_none()

    async def _register_failure(self, account: Account, now: datetime.datetime) -> None:
        """Учитывает неудачную попытку и сохраняет изменение."""
        register_failure(
            account,
            now,
            max_attempts=self._settings.cabinet_max_login_attempts,
            lockout_minutes=self._settings.cabinet_lockout_minutes,
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Сессии
    # ------------------------------------------------------------------

    async def open_session(
        self, account: Account, *, ip_address: str, user_agent: str
    ) -> IssuedSession[AccountSession]:
        """Открывает сессию заказчика."""
        return await self.sessions.open(
            account.id, ip_address=ip_address, user_agent=user_agent
        )

    async def load_session(self, token: str | None) -> AccountSession | None:
        """Находит живую сессию по токену из куки."""
        return await self.sessions.load(token)

    async def close_session(self, token: str | None) -> None:
        """Закрывает сессию."""
        await self.sessions.close(token)

    async def change_password(self, account: Account, *, current: str, new: str) -> int:
        """Меняет пароль и закрывает все открытые сессии.

        :return: сколько сессий было закрыто.
        :raises ValidationError: текущий пароль неверен или новый слаб.
        """
        if account.password_hash is None or not verify_password(
            account.password_hash, current
        ):
            raise ValidationError("Текущий пароль указан неверно.")

        complaint = validate_password_strength(new)
        if complaint is not None:
            raise ValidationError(complaint)

        account.password_hash = hash_password(new)
        await self._db.commit()
        return await self.sessions.close_all(account.id)


async def get_account(db: AsyncSession, account_id: uuid.UUID) -> Account | None:
    """Загружает заказчика по идентификатору."""
    return await db.get(Account, account_id)
