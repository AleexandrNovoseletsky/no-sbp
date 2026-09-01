"""Подключение к базе данных и выдача сессий."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nosbp.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Возвращает движок, создавая его при первом обращении.

    Ленивое создание нужно, чтобы импорт модуля не открывал соединение —
    иначе тесты не смогли бы подменить DSN до старта приложения.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Возвращает фабрику сессий."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


async def dispose_engine() -> None:
    """Закрывает пул соединений. Вызывается при остановке приложения."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession]:
    """Зависимость FastAPI: отдаёт сессию на время одного запроса.

    Коммит делает вызывающий код — так видно, где именно завершается
    транзакция, и не появляется случайных коммитов на пол-операции.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
