"""Вход в панель управления."""

import datetime
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin.security import verify_totp
from nosbp.core.config import Settings
from nosbp.core.errors import AdminAuthError, AdminLockedError
from nosbp.core.lockout import ensure_not_locked, register_failure, reset_failures
from nosbp.core.security import hash_password, password_needs_rehash, verify_password
from nosbp.core.sessions import IssuedSession, SessionStore
from nosbp.db.base import utcnow
from nosbp.db.models import AdminSession, AdminUser

GENERIC_AUTH_MESSAGE: Final[str] = "Неверная почта, пароль или одноразовый код."
"""Единый текст для всех неудач входа.

Различие сообщений позволило бы установить, какие адреса заведены
в системе.
"""

_DUMMY_HASH: Final[str] = hash_password("проверка времени ответа, не пароль")
"""Хэш для несуществующих адресов.

Проверка пароля занимает заметное время; без неё ответ на незаведённый
адрес приходил бы быстрее, и адреса можно было бы перебрать.
"""


class AdminAuthService:
    """Проверяет вход и ведёт сессии администраторов."""

    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self.sessions: SessionStore[AdminSession] = SessionStore(
            db,
            AdminSession,
            ttl_hours=settings.admin_session_ttl_hours,
            idle_minutes=settings.admin_session_idle_minutes,
        )

    async def authenticate(
        self, *, email: str, password: str, totp_code: str | None
    ) -> AdminUser:
        """Проверяет пару «почта — пароль» и одноразовый код.

        :raises AdminLockedError: вход заблокирован после неудачных попыток.
        :raises AdminAuthError: любая другая неудача.
        """
        now = utcnow()
        admin = await self._find_active(email)

        if admin is None:
            # Пароль всё равно проверяется, чтобы время ответа не выдавало,
            # заведён такой адрес или нет.
            verify_password(_DUMMY_HASH, password)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        ensure_not_locked(admin, now, AdminLockedError)

        if not verify_password(admin.password_hash, password):
            await self._register_failure(admin, now)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        if not self._check_totp(admin, totp_code):
            await self._register_failure(admin, now)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        # Параметры argon2 могли усилиться с прошлого входа.
        if password_needs_rehash(admin.password_hash):
            admin.password_hash = hash_password(password)

        reset_failures(admin)
        admin.last_login_at = now
        return admin

    async def _find_active(self, email: str) -> AdminUser | None:
        """Находит действующего администратора по адресу почты."""
        result = await self._db.execute(
            select(AdminUser).where(AdminUser.email == email.strip().lower())
        )
        admin = result.scalar_one_or_none()
        return admin if admin is not None and admin.is_active else None

    def _check_totp(self, admin: AdminUser, code: str | None) -> bool:
        """Проверяет одноразовый код, если второй фактор включён."""
        if not self._settings.admin_require_totp:
            return True
        if admin.totp_secret is None:
            # Второй фактор требуется, но не настроен. Восстанавливается
            # командой «nosbp admin reset-totp».
            return False
        return verify_totp(admin.totp_secret, code or "")

    async def _register_failure(self, admin: AdminUser, now: datetime.datetime) -> None:
        """Учитывает неудачную попытку и сохраняет изменение."""
        register_failure(
            admin,
            now,
            max_attempts=self._settings.admin_max_login_attempts,
            lockout_minutes=self._settings.admin_lockout_minutes,
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Сессии
    # ------------------------------------------------------------------

    async def open_session(
        self, admin: AdminUser, *, ip_address: str, user_agent: str
    ) -> IssuedSession[AdminSession]:
        """Открывает сессию администратора."""
        return await self.sessions.open(
            admin.id, ip_address=ip_address, user_agent=user_agent
        )

    async def load_session(self, token: str | None) -> AdminSession | None:
        """Находит живую сессию по токену из куки."""
        return await self.sessions.load(token)

    async def close_session(self, token: str | None) -> None:
        """Закрывает сессию."""
        await self.sessions.close(token)

    async def close_all_sessions(self, admin_id: uuid.UUID) -> int:
        """Закрывает все сессии администратора."""
        return await self.sessions.close_all(admin_id)
