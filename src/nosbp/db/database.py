"""Подключение к базе данных."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine
)

from nosbp.db.models import Base

# Файл БД лежит в /app/data — эту директорию будем монтировать как volume,
# чтобы данные не терялись при пересборке контейнера
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/nosbp.db")

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency для FastAPI — отдаёт сессию БД на время одного запроса."""
    async with async_session_maker() as session:
        yield session
