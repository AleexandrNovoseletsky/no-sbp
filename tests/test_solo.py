"""Тесты упрощённого сервиса.

Главное, что здесь проверяется, — что упрощённый сервис рисует ровно ту
же картинку, что и полный. Он существует как временная замена, и после
возвращения на основной сервер платёж не должен измениться ни на байт.
"""

import json
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from solo.app import QR_PATH, create_app
from solo.config import ConfigError, Settings, load_settings

from nosbp.payments.gost import build_gost_payload
from nosbp.payments.qr import render_qr_png
from nosbp.payments.schemas import PayeeRequisites, PayerInfo, PaymentRequest
from tests.factories import ORG_DEFAULTS

TOKEN = "kljuch-dlja-testov-dlinnyj-42"
ORG_ALIAS = "main"
SUM_KOPECKS = 4_700_000
PURPOSE = "Заказ 1234"

REQUISITE_FIELDS = {key: value for key, value in ORG_DEFAULTS.items() if key != "alias"}
"""Реквизиты без алиаса: в схеме получателя его нет."""

ENVIRONMENT = {
    "QR_TOKEN": TOKEN,
    "PAYEE_NAME": ORG_DEFAULTS["name"],
    "PAYEE_INN": ORG_DEFAULTS["payee_inn"],
    "PAYEE_KPP": ORG_DEFAULTS["kpp"],
    "PAYEE_ACCOUNT": ORG_DEFAULTS["personal_acc"],
    "PAYEE_BANK_NAME": ORG_DEFAULTS["bank_name"],
    "PAYEE_BIC": ORG_DEFAULTS["bic"],
    "PAYEE_CORR_ACCOUNT": ORG_DEFAULTS["corresp_acc"],
}
"""Полный набор переменных окружения для запуска сервиса."""


def _settings(**overrides: object) -> Settings:
    """Готовая конфигурация для тестов."""
    fields: dict[str, object] = {
        "token": TOKEN,
        "requisites": PayeeRequisites(**REQUISITE_FIELDS),
        "org_alias": ORG_ALIAS,
        "qr_color": "#000000",
        "qr_scale": 10,
        "qr_border": 4,
        "logo": None,
        "cache_max_age_seconds": 2_592_000,
    }
    return Settings(**{**fields, **overrides})  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def solo() -> AsyncIterator[AsyncClient]:
    """Клиент упрощённого сервиса."""
    transport = ASGITransport(app=create_app(_settings()))
    async with AsyncClient(transport=transport, base_url="http://qr.test") as http:
        yield http


def _params(**overrides: object) -> dict[str, object]:
    """Параметры обычного запроса."""
    return {"token": TOKEN, "sum": SUM_KOPECKS, "purpose": PURPOSE, **overrides}


# ---------------------------------------------------------------------------
# Совпадение с полным сервисом
# ---------------------------------------------------------------------------


async def test_picture_matches_the_full_service(solo: AsyncClient) -> None:
    """Ради этого упрощённый сервис и берёт ядро из основного пакета."""
    response = await solo.get(QR_PATH, params=_params())
    assert response.status_code == 200

    payload = build_gost_payload(
        PayeeRequisites(**REQUISITE_FIELDS),
        PaymentRequest(sum_kopecks=SUM_KOPECKS, purpose=PURPOSE, payer=PayerInfo()),
    )
    assert response.content == render_qr_png(payload, color="#000000")


async def test_payer_fields_reach_the_payload(solo: AsyncClient) -> None:
    """Данные плательщика из шаблона CRM должны попадать в строку ГОСТ."""
    with_payer = await solo.get(
        QR_PATH, params=_params(last_name="Иванов", first_name="Иван")
    )
    without_payer = await solo.get(QR_PATH, params=_params())

    assert with_payer.status_code == 200
    assert with_payer.content != without_payer.content


async def test_request_without_a_sum_is_valid(solo: AsyncClient) -> None:
    """Сумму плательщик может ввести в приложении банка сам."""
    response = await solo.get(QR_PATH, params={"token": TOKEN})
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------


async def test_wrong_token_returns_a_readable_picture(solo: AsyncClient) -> None:
    """Код ответа превратился бы в битую иконку в уже отправленном письме."""
    response = await solo.get(QR_PATH, params=_params(token="chuzhoj-kljuch-42"))

    assert response.status_code == 200
    assert response.headers["x-nosbp-error"] == "invalid_token"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"


async def test_missing_token_is_refused(solo: AsyncClient) -> None:
    response = await solo.get(QR_PATH, params={"sum": SUM_KOPECKS})
    assert response.headers["x-nosbp-error"] == "invalid_parameters"


async def test_status_mode_returns_json(solo: AsyncClient) -> None:
    """Для тех, кто вызывает сервис из программы, а не из тега <img>."""
    response = await solo.get(
        QR_PATH, params=_params(token="chuzhoj-kljuch-42", on_error="status")
    )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


async def test_token_comparison_does_not_leak_the_prefix(solo: AsyncClient) -> None:
    """Совпадение начала ключа не должно отличаться от полного промаха."""
    almost = await solo.get(QR_PATH, params=_params(token=TOKEN[:-1] + "x"))
    nothing = await solo.get(QR_PATH, params=_params(token="x"))

    assert almost.headers["x-nosbp-error"] == nothing.headers["x-nosbp-error"]


# ---------------------------------------------------------------------------
# Единственная организация
# ---------------------------------------------------------------------------


async def test_configured_alias_is_accepted(solo: AsyncClient) -> None:
    """Шаблоны CRM уже содержат org — он должен работать как раньше."""
    response = await solo.get(QR_PATH, params=_params(org=ORG_ALIAS))
    assert response.status_code == 200
    assert "x-nosbp-error" not in response.headers


async def test_foreign_alias_is_refused(solo: AsyncClient) -> None:
    """Подставить свои реквизиты вместо запрошенных нельзя.

    Иначе заказчик получил бы QR-код на чужой счёт и заметил бы это
    в лучшем случае после оплаты.
    """
    response = await solo.get(QR_PATH, params=_params(org="drugaya"))
    assert response.headers["x-nosbp-error"] == "organization_not_found"


# ---------------------------------------------------------------------------
# Кэширование и живость
# ---------------------------------------------------------------------------


async def test_repeat_request_is_not_redrawn(solo: AsyncClient) -> None:
    first = await solo.get(QR_PATH, params=_params())
    etag = first.headers["etag"]

    again = await solo.get(QR_PATH, params=_params(), headers={"If-None-Match": etag})

    assert again.status_code == 304
    assert not again.content


async def test_different_payments_get_different_etags(solo: AsyncClient) -> None:
    first = await solo.get(QR_PATH, params=_params())
    second = await solo.get(QR_PATH, params=_params(sum=SUM_KOPECKS + 100))

    assert first.headers["etag"] != second.headers["etag"]


async def test_health_answers(solo: AsyncClient) -> None:
    response = await solo.get("/health")
    assert response.json() == {"status": "ok"}


async def test_documentation_is_not_served(solo: AsyncClient) -> None:
    """Сервис стоит на публичном адресе, описывать в нём нечего."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await solo.get(path)).status_code == 404


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


def test_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    settings = load_settings()

    assert settings.token == TOKEN
    assert settings.requisites.bic == ORG_DEFAULTS["bic"]
    assert settings.org_alias == "main"


@pytest.mark.parametrize(
    ("variable", "value", "complaint"),
    [
        ("PAYEE_ACCOUNT", "40702810000000000001", "контрольн"),
        ("PAYEE_BIC", "04452522", "БИК"),
        ("PAYEE_INN", "7707083890", "ИНН"),
        ("PAYEE_KPP", "77360100", "КПП"),
    ],
)
def test_broken_requisites_stop_the_service(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, complaint: str
) -> None:
    """Опечатка в реквизитах должна всплыть при развёртывании.

    Сервис, поднявшийся с неверным счётом, хуже не поднявшегося:
    клиент оплатит в никуда.
    """
    for name, default in ENVIRONMENT.items():
        monkeypatch.setenv(name, default)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigError, match=complaint):
        load_settings()


def test_missing_variable_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PAYEE_BANK_NAME")

    with pytest.raises(ConfigError, match="PAYEE_BANK_NAME"):
        load_settings()


def test_short_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Короткий ключ в адресе картинки подбирается перебором."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("QR_TOKEN", "korotkij")

    with pytest.raises(ConfigError, match="QR_TOKEN"):
        load_settings()


def test_pale_colour_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Такой код не отсканируется, и узнать об этом лучше сразу."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("QR_COLOR", "#ffee00")

    with pytest.raises(ConfigError, match="QR_COLOR"):
        load_settings()


def test_missing_logo_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LOGO_PATH", "/app/logo/net-takogo-fajla.png")

    with pytest.raises(ConfigError, match="не найден"):
        load_settings()


def test_nonsense_number_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("QR_SCALE", "очень большой")

    with pytest.raises(ConfigError, match="QR_SCALE"):
        load_settings()


# ---------------------------------------------------------------------------
# Состав образа
# ---------------------------------------------------------------------------

DOCKERFILE = Path(__file__).resolve().parent.parent / "solo" / "Dockerfile"
COPIED_MODULE = re.compile(r"src/(nosbp/[\w/]+)\.py")


def _modules_in_dockerfile() -> set[str]:
    """Модули основного пакета, которые копируются в образ.

    Файл ``__init__.py`` даёт имя самого пакета, а не модуля внутри него:
    ``nosbp/core/__init__.py`` — это ``nosbp.core``.
    """
    return {
        match.replace("/", ".").removesuffix(".__init__")
        for match in COPIED_MODULE.findall(DOCKERFILE.read_text(encoding="utf-8"))
    }


def _modules_actually_imported() -> set[str]:
    """Модули основного пакета, которые нужны упрощённому сервису.

    Считаются в отдельном процессе: в самих тестах половина пакета уже
    импортирована обвязкой, и разобрать, кому что нужно, стало бы нельзя.
    """
    code = (
        "import solo.app, sys, json; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('nosbp'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=DOCKERFILE.parent.parent,
    )
    return set(json.loads(result.stdout))


def test_image_carries_every_module_the_service_imports() -> None:
    """Список файлов в Dockerfile не должен отставать от импортов.

    Новый импорт в solo, забытый в Dockerfile, уронил бы контейнер
    на старте — но только на сервере и только после развёртывания.
    """
    missing = _modules_actually_imported() - _modules_in_dockerfile()
    assert not missing, "в solo/Dockerfile не хватает COPY для модулей: " + ", ".join(
        sorted(missing)
    )


def test_service_needs_neither_database_nor_storage() -> None:
    """На этом держится весь размер образа: четыре зависимости вместо пятнадцати."""
    code = (
        "import solo.app, sys; "
        "heavy = [m for m in ('sqlalchemy', 'asyncpg', 'boto3', 'alembic', 'argon2') "
        "if m in sys.modules]; "
        "assert not heavy, heavy"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        cwd=DOCKERFILE.parent.parent,
    )
