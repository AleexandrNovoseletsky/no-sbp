import asyncio
import secrets

import pytest


@pytest.fixture(scope="session")
def temp_db_url(tmp_path_factory, monkeypatch):
    """Создаёт временную sqlite-базу и устанавливает DATABASE_URL для тестовой сессии.

    Устанавливаем переменную окружения до импорта приложения, чтобы
    модуль src.nosbp.db.database использовал правильный URL при создании engine.
    """
    db_dir = tmp_path_factory.mktemp("db")
    db_file = db_dir / "nosbp_test.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture(scope="session")
def test_app(temp_db_url):
    """Импортирует приложение после переопределения DATABASE_URL и инициализирует БД."""
    # Импортировать приложение после того, как DATABASE_URL установлен
    from src.nosbp.main import app
    from src.nosbp.db.database import init_db

    asyncio.run(init_db())
    return app


@pytest.fixture
def client(test_app):
    """Sync TestClient для взаимодействия с приложением в тестах."""
    from fastapi.testclient import TestClient

    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def create_client():
    """Фабрика для создания записи Client в тестовой БД. Возвращает api_key.

    Используется как синхронный helper в тестах (внутри вызывает asyncio.run).
    """

    def _create(api_key: str | None = None) -> str:
        if api_key is None:
            api_key = secrets.token_urlsafe(32)

        async def _do():
            from src.nosbp.db.database import async_session_maker
            from src.nosbp.db.models import Client

            client_obj = Client(
                api_key=api_key,
                display_name="TestClient",
                name="ООО Тест",
                personal_acc="1" * 20,
                bank_name="ТестБанк",
                bic="0" * 9,
                corresp_acc="2" * 20,
                payee_inn="1" * 10,
            )

            async with async_session_maker() as session:
                session.add(client_obj)
                await session.commit()

        asyncio.run(_do())
        return api_key

    return _create
