"""Тесты публичной страницы оплаты.

Страница показывает плательщику готовый счёт: сумму, QR-код и реквизиты
текстом. Она раскрывает банковские реквизиты получателя, поэтому по
умолчанию выключена — это первое, что здесь проверяется.
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from nosbp.core.config import get_settings
from nosbp.db.models import LedgerEntry
from nosbp.mail.service import Mailer
from nosbp.main import create_app, prepare_state
from nosbp.payments.routes import INVOICE_HEADER, QR_PATH
from tests.factories import BIC, CORRESP_ACC, PERSONAL_ACC

PAY_PREFIX = "/pay"
SUM_KOPECKS = 4_700_000
PURPOSE = "Оплата заказа 1001"


@pytest_asyncio.fixture
async def pay(storage, mailbox):
    """Клиент приложения с включённой страницей оплаты."""
    settings = get_settings().model_copy(update={"payment_page_enabled": True})
    app = create_app(settings)
    app.state.storage = storage
    prepare_state(app, settings)
    app.state.mailer = Mailer(mailbox, base_url=settings.public_base_url)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://pay.test",
        follow_redirects=False,
    ) as http:
        yield http


async def _make_invoice(client, token: str, **overrides) -> str:
    """Создаёт счёт через API и возвращает его публичный идентификатор."""
    params = {
        "token": token,
        "sum": SUM_KOPECKS,
        "purpose": PURPOSE,
        **overrides,
    }
    response = await client.get(QR_PATH, params=params)
    assert response.status_code == 200, response.text
    return response.headers[INVOICE_HEADER]


# ---------------------------------------------------------------------------
# Выключена по умолчанию
# ---------------------------------------------------------------------------


async def test_page_is_absent_until_it_is_turned_on(client, merchant):
    """Пока расчётный счёт не открыт, реквизиты показывать нечего."""
    _, _, token = merchant
    public_token = await _make_invoice(client, token)

    response = await client.get(f"{PAY_PREFIX}/{public_token}")
    assert response.status_code == 404


async def test_qr_endpoint_is_absent_too(client, merchant):
    _, _, token = merchant
    public_token = await _make_invoice(client, token)

    response = await client.get(f"{PAY_PREFIX}/{public_token}/qr.png")
    assert response.status_code == 404


def test_setting_is_off_by_default():
    assert get_settings().payment_page_enabled is False


# ---------------------------------------------------------------------------
# Включённая страница
# ---------------------------------------------------------------------------


async def test_page_shows_amount_purpose_and_requisites(client, pay, merchant):
    _account, organization, token = merchant
    public_token = await _make_invoice(client, token)

    response = await pay.get(f"{PAY_PREFIX}/{public_token}")

    assert response.status_code == 200
    assert organization.name in response.text
    assert PURPOSE in response.text
    assert "47000.00 ₽" in response.text
    assert PERSONAL_ACC in response.text
    assert CORRESP_ACC in response.text
    assert BIC in response.text


async def test_page_links_to_its_own_picture(client, pay, merchant):
    _, _, token = merchant
    public_token = await _make_invoice(client, token)

    response = await pay.get(f"{PAY_PREFIX}/{public_token}")
    assert f"{PAY_PREFIX}/{public_token}/qr.png" in response.text


async def test_picture_is_a_png(client, pay, merchant):
    _, _, token = merchant
    public_token = await _make_invoice(client, token)

    response = await pay.get(f"{PAY_PREFIX}/{public_token}/qr.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


async def test_invoice_without_a_sum_says_so(client, pay, merchant):
    """Плательщик вводит сумму сам — страница должна это объяснить."""
    _, _, token = merchant
    response = await client.get(QR_PATH, params={"token": token})
    public_token = response.headers[INVOICE_HEADER]

    page = await pay.get(f"{PAY_PREFIX}/{public_token}")
    assert "Сумму укажете в приложении банка" in page.text


async def test_unknown_token_gets_an_explanation(pay):
    response = await pay.get(f"{PAY_PREFIX}/vydumannyj")

    assert response.status_code == 404
    assert "Счёт не найден" in response.text


async def test_unknown_token_has_no_picture(pay):
    response = await pay.get(f"{PAY_PREFIX}/vydumannyj/qr.png")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Что страница не должна делать
# ---------------------------------------------------------------------------


async def test_page_does_not_charge_anything(client, pay, merchant, session):
    """Счёт оплачен заказчиком при генерации: просмотр стоит ноль."""
    account, _, token = merchant
    public_token = await _make_invoice(client, token)

    before = await _charge_count(session, account.id)
    for _ in range(3):
        await pay.get(f"{PAY_PREFIX}/{public_token}")
        await pay.get(f"{PAY_PREFIX}/{public_token}/qr.png")

    assert await _charge_count(session, account.id) == before


async def _charge_count(session, account_id) -> int:
    """Сколько операций в журнале у аккаунта."""
    result = await session.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.account_id == account_id)
    )
    return int(result.scalar_one())


async def test_page_is_not_cached_and_not_indexed(client, pay, merchant):
    _, _, token = merchant
    public_token = await _make_invoice(client, token)

    for path in (f"{PAY_PREFIX}/{public_token}", f"{PAY_PREFIX}/{public_token}/qr.png"):
        response = await pay.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"


async def test_stylesheet_is_served(pay):
    """Встроенные стили запрещены политикой безопасности — нужен файл."""
    response = await pay.get(f"{PAY_PREFIX}/static/pay.css")

    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
