"""Общая обвязка тестов.

Тесты работают против настоящего PostgreSQL — того же, что и продакшен.
SQLite здесь не подошёл бы: блокировка строки ``SELECT ... FOR UPDATE``,
на которой держится корректность списаний, в SQLite не работает, и
тест на параллельные запросы стал бы бессмысленным.

База поднимается командой ``docker compose up -d postgres``.
"""

import os
import uuid
from collections.abc import AsyncIterator

# Настройки читаются при первом обращении, поэтому подменять адрес базы
# нужно до импорта любого модуля приложения.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://nosbp:nosbp@localhost:55432/nosbp_test",
)
os.environ["ENVIRONMENT"] = "local"
os.environ["S3_ACCESS_KEY"] = "test"
os.environ["S3_SECRET_KEY"] = "test"

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.billing.service import BillingService
from nosbp.core.config import Settings, get_settings
from nosbp.db.base import Base
from nosbp.db.models import Account, ApiToken, Organization
from nosbp.db.session import (
    dispose_engine,
    get_engine,
    get_session_factory,
)
from nosbp.main import create_app
from nosbp.payments.tokens import generate_token, hash_token, token_prefix
from nosbp.storage.logo_cache import LogoCache
from nosbp.storage.memory import MemoryStorage
from tests.factories import ORG_DEFAULTS

TABLES = (
    "ledger_entries",
    "invoices",
    "api_tokens",
    "organizations",
    "accounts",
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    """Создаёт схему один раз на весь прогон и убирает её за собой."""
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(_schema: None) -> AsyncIterator[None]:
    """Очищает таблицы перед каждым тестом.

    TRUNCATE, а не пересоздание схемы: так на порядок быстрее, а тесты
    остаются полностью изолированными друг от друга.
    """
    async with get_session_factory()() as session:
        await session.execute(
            text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield


@pytest.fixture
def settings() -> Settings:
    """Настройки приложения в тестовом окружении."""
    return get_settings()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Сессия базы для подготовки данных и проверок в тестах."""
    async with get_session_factory()() as db_session:
        yield db_session


@pytest.fixture
def storage() -> MemoryStorage:
    """Хранилище в памяти вместо S3."""
    return MemoryStorage()


@pytest_asyncio.fixture
async def client(storage: MemoryStorage) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх приложения, без реального сетевого сокета.

    Ресурсы, которые обычно создаёт lifespan, подставляются вручную —
    так тесты не ходят в S3 и не зависят от порядка запуска.
    """
    app = create_app()
    app.state.storage = storage
    app.state.logo_cache = LogoCache(storage)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ---------------------------------------------------------------------------
# Фабрики данных
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def make_account(session: AsyncSession):
    """Фабрика аккаунтов с произвольным начальным балансом."""

    async def _make(
        *,
        balance_rubles: int = 100,
        daily_limit_rubles: int | None = None,
        email: str | None = None,
    ) -> Account:
        account = Account(
            email=email or f"{uuid.uuid4().hex[:8]}@example.com",
            display_name="Тестовый заказчик",
            balance_kopecks=0,
            daily_charge_limit_kopecks=(
                None if daily_limit_rubles is None else daily_limit_rubles * 100
            ),
        )
        session.add(account)
        await session.flush()

        if balance_rubles:
            # Корректировка, а не пополнение: минимальная сумма пополнения
            # в 1000 ₽ — правило для заказчиков, а не для тестовых данных.
            billing = BillingService(session, get_settings())
            await billing.adjust(
                account, balance_rubles * 100, comment="Стартовый баланс"
            )
        await session.commit()
        return account

    return _make


@pytest_asyncio.fixture
async def make_organization(session: AsyncSession):
    """Фабрика организаций-получателей платежа."""

    async def _make(account: Account, **overrides: object) -> Organization:
        fields = {**ORG_DEFAULTS, **overrides}
        organization = Organization(account_id=account.id, is_default=True, **fields)
        session.add(organization)
        await session.commit()
        return organization

    return _make


@pytest_asyncio.fixture
async def make_token(session: AsyncSession):
    """Фабрика ключей API. Возвращает сам токен в открытом виде."""

    async def _make(account: Account, *, label: str = "test") -> str:
        token = generate_token()
        session.add(
            ApiToken(
                account_id=account.id,
                token_hash=hash_token(token),
                prefix=token_prefix(token),
                label=label,
            )
        )
        await session.commit()
        return token

    return _make


@pytest_asyncio.fixture
async def merchant(make_account, make_organization, make_token):
    """Готовый заказчик: аккаунт с балансом, организация и рабочий ключ.

    Возвращает кортеж ``(account, organization, token)`` — то, что нужно
    почти каждому тесту API.
    """
    account = await make_account()
    organization = await make_organization(account)
    token = await make_token(account)
    return account, organization, token
