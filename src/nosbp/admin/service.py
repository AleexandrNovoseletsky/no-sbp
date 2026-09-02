"""Вход в панель управления и работа с сессиями."""

import datetime
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin.security import (
    generate_csrf_token,
    generate_session_token,
    hash_password,
    hash_session_token,
    password_needs_rehash,
    verify_password,
    verify_totp,
)
from nosbp.core.config import Settings
from nosbp.core.errors import AdminAuthError, AdminLockedError
from nosbp.db.base import utcnow
from nosbp.db.models import AdminSession, AdminUser

GENERIC_AUTH_MESSAGE: Final[str] = "Неверная почта, пароль или одноразовый код."
"""Один текст на все неудачи входа.

Если различать «нет такого адреса» и «неверный пароль», по ответам можно
собрать список заведённых администраторов.
"""


@dataclass(frozen=True)
class IssuedSession:
    """Выданная сессия: токен для куки и запись в базе."""

    token: str
    session: AdminSession


class AdminAuthService:
    """Проверяет вход и ведёт сессии администраторов."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

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
            # Пароль всё равно «проверяем», чтобы время ответа не выдавало,
            # заведён такой адрес или нет.
            verify_password(_DUMMY_HASH, password)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        self._ensure_not_locked(admin, now)

        if not verify_password(admin.password_hash, password):
            await self._register_failure(admin, now)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        if not self._check_totp(admin, totp_code):
            await self._register_failure(admin, now)
            raise AdminAuthError(GENERIC_AUTH_MESSAGE)

        # Параметры argon2 могли усилиться с прошлого входа — пересчитываем.
        if password_needs_rehash(admin.password_hash):
            admin.password_hash = hash_password(password)

        admin.failed_attempts = 0
        admin.locked_until = None
        admin.last_login_at = now
        return admin

    async def _find_active(self, email: str) -> AdminUser | None:
        """Находит действующего администратора по адресу почты."""
        result = await self._session.execute(
            select(AdminUser).where(AdminUser.email == email.strip().lower())
        )
        admin = result.scalar_one_or_none()
        return admin if admin is not None and admin.is_active else None

    def _ensure_not_locked(self, admin: AdminUser, now: datetime.datetime) -> None:
        """Проверяет, что вход не заблокирован."""
        if admin.locked_until is not None and now < admin.locked_until:
            minutes_left = max(
                1, int((admin.locked_until - now).total_seconds() // 60) + 1
            )
            raise AdminLockedError(
                f"Слишком много неудачных попыток. Повторите через {minutes_left} мин."
            )

    def _check_totp(self, admin: AdminUser, code: str | None) -> bool:
        """Проверяет одноразовый код, если второй фактор включён."""
        if not self._settings.admin_require_totp:
            return True
        if admin.totp_secret is None:
            # Второй фактор требуется, но не настроен — вход невозможен.
            # Чинится командой «nosbp admin reset-totp».
            return False
        return verify_totp(admin.totp_secret, code or "")

    async def _register_failure(self, admin: AdminUser, now: datetime.datetime) -> None:
        """Считает неудачную попытку и при переполнении блокирует вход."""
        admin.failed_attempts += 1
        if admin.failed_attempts >= self._settings.admin_max_login_attempts:
            admin.locked_until = now + datetime.timedelta(
                minutes=self._settings.admin_lockout_minutes
            )
            admin.failed_attempts = 0
        await self._session.commit()

    # ------------------------------------------------------------------
    # Сессии
    # ------------------------------------------------------------------

    async def open_session(
        self, admin: AdminUser, *, ip_address: str, user_agent: str
    ) -> IssuedSession:
        """Открывает новую сессию и возвращает токен для куки."""
        token = generate_session_token()
        now = utcnow()

        session = AdminSession(
            admin_id=admin.id,
            token_hash=hash_session_token(token),
            csrf_token=generate_csrf_token(),
            expires_at=now
            + datetime.timedelta(hours=self._settings.admin_session_ttl_hours),
            last_seen_at=now,
            ip_address=ip_address[:45],
            user_agent=user_agent[:1000],
        )
        self._session.add(session)
        await self._session.commit()
        return IssuedSession(token=token, session=session)

    async def load_session(self, token: str | None) -> AdminSession | None:
        """Находит живую сессию по токену из куки.

        Продлевает отметку активности: сессия закрывается не только по
        общему сроку, но и по бездействию.
        """
        if not token:
            return None

        result = await self._session.execute(
            select(AdminSession).where(
                AdminSession.token_hash == hash_session_token(token)
            )
        )
        session = result.scalar_one_or_none()
        if session is None or session.revoked_at is not None:
            return None

        now = utcnow()
        idle_limit = datetime.timedelta(
            minutes=self._settings.admin_session_idle_minutes
        )
        if now >= session.expires_at or now - session.last_seen_at > idle_limit:
            session.revoked_at = now
            await self._session.commit()
            return None

        session.last_seen_at = now
        await self._session.commit()
        return session

    async def close_session(self, token: str | None) -> None:
        """Отзывает сессию — выход из панели."""
        session = await self.load_session(token)
        if session is None:
            return
        session.revoked_at = utcnow()
        await self._session.commit()

    async def close_all_sessions(self, admin_id: object) -> int:
        """Отзывает все сессии администратора. Нужно при смене пароля."""
        result = await self._session.execute(
            select(AdminSession).where(
                AdminSession.admin_id == admin_id,
                AdminSession.revoked_at.is_(None),
            )
        )
        now = utcnow()
        sessions = list(result.scalars())
        for session in sessions:
            session.revoked_at = now
        await self._session.commit()
        return len(sessions)


_DUMMY_HASH: Final[str] = hash_password("проверка времени ответа, не пароль")
"""Хэш-пустышка для несуществующих адресов.

Проверка пароля занимает заметное время; без неё ответ на незаведённый
адрес приходил бы быстрее, и адреса можно было бы перебрать.
"""
