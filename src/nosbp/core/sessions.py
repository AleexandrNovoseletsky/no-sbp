"""Управление сессиями веб-интерфейсов.

Логика одинакова для панели управления и личного кабинета: сессия
хранится в базе, продлевается при обращении и закрывается либо по общему
сроку, либо по бездействию. Конкретная модель передаётся параметром.
"""

import datetime
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.constants import IP_ADDRESS_MAX_LENGTH, USER_AGENT_MAX_LENGTH
from nosbp.core.security import (
    generate_csrf_token,
    generate_session_token,
    hash_session_token,
)
from nosbp.db.base import utcnow
from nosbp.db.models import WebSession

MINUTES_IN_HOUR: Final = 60


@dataclass(frozen=True)
class IssuedSession[M: WebSession]:
    """Выданная сессия: токен для куки и запись в базе."""

    token: str
    session: M


class SessionStore[M: WebSession]:
    """Хранилище сессий одной модели."""

    def __init__(
        self,
        db: AsyncSession,
        model: type[M],
        *,
        ttl_hours: int,
        idle_minutes: int,
    ) -> None:
        self._db = db
        self._model = model
        self._ttl = datetime.timedelta(hours=ttl_hours)
        self._idle = datetime.timedelta(minutes=idle_minutes)

    async def open(
        self, owner_id: uuid.UUID, *, ip_address: str, user_agent: str
    ) -> IssuedSession[M]:
        """Открывает сессию и возвращает токен для куки."""
        token = generate_session_token()
        now = utcnow()

        session = self._model(
            owner_id=owner_id,
            token_hash=hash_session_token(token),
            csrf_token=generate_csrf_token(),
            expires_at=now + self._ttl,
            last_seen_at=now,
            ip_address=ip_address[:IP_ADDRESS_MAX_LENGTH],
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH],
        )
        self._db.add(session)
        await self._purge_stale(owner_id, now)
        await self._db.commit()
        return IssuedSession(token=token, session=session)

    async def load(self, token: str | None) -> M | None:
        """Находит живую сессию по токену из куки.

        Отметка активности продлевается: сессия закрывается не только
        по общему сроку, но и по бездействию.
        """
        if not token:
            return None

        result = await self._db.execute(
            select(self._model).where(
                self._model.token_hash == hash_session_token(token)
            )
        )
        session = result.scalar_one_or_none()
        if session is None or session.revoked_at is not None:
            return None

        now = utcnow()
        if now >= session.expires_at or now - session.last_seen_at > self._idle:
            session.revoked_at = now
            await self._db.commit()
            return None

        session.last_seen_at = now
        await self._db.commit()
        return session

    async def close(self, token: str | None) -> None:
        """Отзывает сессию — выход из интерфейса."""
        session = await self.load(token)
        if session is None:
            return
        session.revoked_at = utcnow()
        await self._db.commit()

    async def close_all(self, owner_id: uuid.UUID) -> int:
        """Отзывает все сессии владельца. Нужно при смене пароля."""
        result = await self._db.execute(
            select(self._model).where(
                self._model.owner_id == owner_id,
                self._model.revoked_at.is_(None),
            )
        )
        now = utcnow()
        sessions = list(result.scalars())
        for session in sessions:
            session.revoked_at = now
        await self._db.commit()
        return len(sessions)

    async def _purge_stale(self, owner_id: uuid.UUID, now: datetime.datetime) -> None:
        """Удаляет истёкшие и отозванные сессии владельца.

        Выполняется при входе: количество сессий одного пользователя
        невелико, и отдельный планировщик для этого не требуется. Без
        уборки таблица растёт неограниченно.
        """
        await self._db.execute(
            delete(self._model).where(
                self._model.owner_id == owner_id,
                or_(
                    self._model.expires_at < now,
                    self._model.revoked_at.is_not(None),
                ),
            )
        )
