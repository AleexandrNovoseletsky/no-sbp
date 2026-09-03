"""Тесты защитных механизмов уровня приложения."""

import pytest
from httpx import ASGITransport, AsyncClient

from nosbp.admin.dependencies import build_templates
from nosbp.core.config import Settings, get_settings
from nosbp.core.middleware import CONTENT_SECURITY_POLICY, SECURITY_HEADERS
from nosbp.main import build_logo_cache, create_app
from nosbp.storage.memory import MemoryStorage
from tests.factories import ADMIN_PREFIX


def make_client(settings: Settings) -> AsyncClient:
    """Поднимает приложение с заданными настройками."""
    app = create_app(settings)
    app.state.storage = MemoryStorage()
    app.state.logo_cache = build_logo_cache(app, settings)
    app.state.admin_templates = build_templates(settings)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://panel.test",
        follow_redirects=False,
    )


def settings_with(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Заголовки
# ---------------------------------------------------------------------------


async def test_security_headers_are_present(panel):
    response = await panel.get(f"{ADMIN_PREFIX}/login")
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value


async def test_content_security_policy_forbids_inline_code(panel):
    """Внедрённый на страницу скрипт не должен выполниться."""
    response = await panel.get(f"{ADMIN_PREFIX}/login")
    policy = response.headers["Content-Security-Policy"]
    assert policy == CONTENT_SECURITY_POLICY
    assert "'unsafe-inline'" not in policy
    assert "script-src 'self'" in policy


async def test_admin_pages_have_no_inline_scripts_or_styles(panel):
    """Иначе страница перестала бы работать под собственной же политикой."""
    response = await panel.get(f"{ADMIN_PREFIX}/login")
    assert "<script>" not in response.text
    assert "onsubmit=" not in response.text
    assert "style=" not in response.text


async def test_hsts_only_in_production():
    async with make_client(settings_with(environment="local")) as client:
        local = await client.get("/health")
    async with make_client(settings_with(environment="production")) as client:
        production = await client.get("/health")

    assert "Strict-Transport-Security" not in local.headers
    assert "Strict-Transport-Security" in production.headers


# ---------------------------------------------------------------------------
# Документация API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_hidden_in_production(path: str):
    """Схема раскрывает состав эндпоинтов и их параметры."""
    async with make_client(settings_with(environment="production")) as client:
        response = await client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_api_docs_available_locally(path: str):
    async with make_client(settings_with(environment="local")) as client:
        response = await client.get(path)
    assert response.status_code == 200


async def test_docs_can_be_forced_on(path: str = "/docs"):
    async with make_client(
        settings_with(environment="production", enable_api_docs=True)
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Ограничение доступа к панели по сети
# ---------------------------------------------------------------------------


async def test_panel_is_hidden_from_foreign_networks():
    settings = settings_with(admin_allowed_networks="10.0.0.0/8")
    async with make_client(settings) as client:
        response = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "203.0.113.7"}
        )
    # Ответ совпадает с ответом на несуществующий путь.
    assert response.status_code == 404


async def test_panel_opens_from_allowed_network():
    settings = settings_with(admin_allowed_networks="10.0.0.0/8, 203.0.113.7")
    async with make_client(settings) as client:
        response = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "10.1.2.3"}
        )
    assert response.status_code == 200


async def test_single_address_is_allowed():
    settings = settings_with(admin_allowed_networks="203.0.113.7")
    async with make_client(settings) as client:
        allowed = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "203.0.113.7"}
        )
        denied = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "203.0.113.8"}
        )
    assert allowed.status_code == 200
    assert denied.status_code == 404


async def test_restriction_does_not_touch_the_api():
    """Генерация QR-кодов должна работать откуда угодно."""
    settings = settings_with(admin_allowed_networks="10.0.0.0/8")
    async with make_client(settings) as client:
        response = await client.get(
            "/health", headers={"X-Forwarded-For": "203.0.113.7"}
        )
    assert response.status_code == 200


async def test_empty_list_means_no_restriction():
    settings = settings_with(admin_allowed_networks="")
    async with make_client(settings) as client:
        response = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "203.0.113.7"}
        )
    assert response.status_code == 200


async def test_malformed_address_is_denied():
    settings = settings_with(admin_allowed_networks="10.0.0.0/8")
    async with make_client(settings) as client:
        response = await client.get(
            f"{ADMIN_PREFIX}/login", headers={"X-Forwarded-For": "not-an-address"}
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Путь панели
# ---------------------------------------------------------------------------


async def test_admin_path_is_configurable():
    settings = settings_with(admin_path_prefix="/very-secret-place")
    async with make_client(settings) as client:
        moved = await client.get("/very-secret-place/login")
        old = await client.get("/console/login")

    assert moved.status_code == 200
    assert old.status_code == 404


@pytest.mark.parametrize("bad", ["", "/", "console", "/console/", "/con sole"])
def test_malformed_admin_path_is_refused(bad: str):
    """Кривой путь должен ломать запуск, а не тихо разъезжаться со ссылками."""
    with pytest.raises(ValueError):
        Settings(admin_path_prefix=bad)


# ---------------------------------------------------------------------------
# Статика панели
# ---------------------------------------------------------------------------


async def test_static_files_are_served(panel):
    for name in ("admin.css", "admin.js", "favicon.svg"):
        response = await panel.get(f"{ADMIN_PREFIX}/static/{name}")
        assert response.status_code == 200, name
        assert response.content
