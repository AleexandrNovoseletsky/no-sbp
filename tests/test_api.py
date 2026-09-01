"""Сквозные тесты API генерации QR-кода."""

import asyncio
import datetime

from sqlalchemy import func, select

from nosbp.billing.service import calculate_balance_from_ledger
from nosbp.db.models import Invoice, LedgerEntry, LedgerEntryType
from nosbp.payments.routes import ERROR_HEADER
from tests.factories import CORRESP_ACC, INN_COMPANY, PERSONAL_ACC
from tests.qrdecode import decode_qr

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def qr_url(token: str, **params: object) -> str:
    """Собирает адрес, который заказчик вставляет в тег <img>."""
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/v1/qr?token={token}" + (f"&{query}" if query else "")


async def test_health_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Основной сценарий
# ---------------------------------------------------------------------------


async def test_get_returns_png(client, merchant):
    _, _, token = merchant
    response = await client.get(qr_url(token, sum=4700000, purpose="Заказ+1234"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(PNG_MAGIC)


async def test_served_qr_actually_scans_and_carries_requisites(client, merchant):
    """Сквозная проверка: то, что уехало клиенту, читается сканером.

    Все остальные тесты проверяют куски по отдельности — этот один
    отвечает на вопрос «а заплатить по этой картинке вообще можно?».
    """
    _, _, token = merchant
    response = await client.get(qr_url(token, sum=4700000, purpose="Order+1234"))

    decoded = decode_qr(response.content)
    assert decoded is not None, "выданный QR-код не распознаётся"
    assert decoded.startswith("ST00012|")
    assert f"PersonalAcc={PERSONAL_ACC}" in decoded
    assert f"CorrespAcc={CORRESP_ACC}" in decoded
    assert f"PayeeINN={INN_COMPANY}" in decoded
    assert "Sum=4700000" in decoded


async def test_post_returns_png(client, merchant):
    _, _, token = merchant
    response = await client.post(
        "/v1/qr",
        headers={"Authorization": f"Bearer {token}"},
        json={"sum": 4700000, "purpose": "Заказ 1234"},
    )
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)


async def test_request_without_sum_is_valid(client, merchant):
    """Сумму можно не указывать — плательщик введёт её в банке сам."""
    _, _, token = merchant
    response = await client.get(qr_url(token))
    assert response.status_code == 200


async def test_organization_can_be_chosen_by_alias(client, merchant, make_organization):
    """У заказчика может быть несколько юрлиц — выбор через параметр org."""
    account, _, token = merchant
    await make_organization(account, alias="second", name="ИП Иванов")

    first = await client.get(qr_url(token, sum=1000))
    second = await client.get(qr_url(token, org="second", sum=1000))

    assert first.status_code == second.status_code == 200
    # Разные реквизиты — разные счета и разные картинки.
    assert first.headers["X-NoSBP-Invoice"] != second.headers["X-NoSBP-Invoice"]
    assert first.content != second.content


async def test_unknown_organization_alias_is_reported(client, merchant):
    _, _, token = merchant
    response = await client.get(qr_url(token, org="missing", on_error="status"))
    assert response.status_code == 404
    assert response.json()["error"] == "organization_not_found"


# ---------------------------------------------------------------------------
# Тарификация: платим за счёт, а не за обращение
# ---------------------------------------------------------------------------


async def test_first_request_charges_one_rouble(client, merchant, session):
    account, _, token = merchant
    start = account.balance_kopecks

    response = await client.get(qr_url(token, sum=4700000))
    assert response.headers["X-NoSBP-Charged"] == "1"

    await session.refresh(account)
    assert account.balance_kopecks == start - 100


async def test_repeated_identical_request_is_free(client, merchant, session):
    """Письмо открыли пять раз — заказчик платит за один счёт.

    Это главное правило тарификации: предзагрузка картинок почтовыми
    клиентами не должна тарифицироваться как новые счета.
    """
    account, _, token = merchant
    start = account.balance_kopecks
    url = qr_url(token, sum=4700000, purpose="Заказ+1234")

    charged_flags = []
    for _ in range(5):
        response = await client.get(url)
        assert response.status_code == 200
        charged_flags.append(response.headers["X-NoSBP-Charged"])

    assert charged_flags == ["1", "0", "0", "0", "0"]
    await session.refresh(account)
    assert account.balance_kopecks == start - 100


async def test_sixty_invoices_cost_sixty_roubles(client, merchant, session):
    """Отправили 60 счетов — списано ровно 60 рублей."""
    account, _, token = merchant
    start = account.balance_kopecks

    for number in range(60):
        response = await client.get(qr_url(token, sum=100000 + number))
        assert response.status_code == 200

    await session.refresh(account)
    assert account.balance_kopecks == start - 60 * 100


async def test_different_payers_are_different_invoices(client, merchant, session):
    account, _, token = merchant
    start = account.balance_kopecks

    await client.get(qr_url(token, sum=1000, last_name="Иванов"))
    await client.get(qr_url(token, sum=1000, last_name="Петров"))

    await session.refresh(account)
    assert account.balance_kopecks == start - 200


async def test_expired_free_period_charges_again(client, merchant, session):
    """Заказчик вернулся к счёту через месяц — рубль списывается заново."""
    _, _, token = merchant
    url = qr_url(token, sum=4700000, purpose="Заказ+777")

    await client.get(url)
    invoice = (
        await session.execute(select(Invoice).where(Invoice.sum_kopecks == 4700000))
    ).scalar_one()
    invoice.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        seconds=1
    )
    await session.commit()

    response = await client.get(url)
    assert response.headers["X-NoSBP-Charged"] == "1"

    await session.refresh(invoice)
    assert invoice.charge_count == 2


async def test_hit_count_counts_every_request(client, merchant, session):
    _, _, token = merchant
    url = qr_url(token, sum=555)
    for _ in range(4):
        await client.get(url)

    invoice = (
        await session.execute(select(Invoice).where(Invoice.sum_kopecks == 555))
    ).scalar_one()
    assert invoice.hit_count == 4
    assert invoice.charge_count == 1


async def test_concurrent_identical_requests_charge_once(client, merchant, session):
    """Гонка двух одновременных запросов не должна списать два рубля."""
    account, _, token = merchant
    start = account.balance_kopecks
    url = qr_url(token, sum=987654, purpose="Гонка")

    responses = await asyncio.gather(*(client.get(url) for _ in range(6)))
    assert all(response.status_code == 200 for response in responses)

    await session.refresh(account)
    assert account.balance_kopecks == start - 100

    charges = await session.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.account_id == account.id,
            LedgerEntry.entry_type == LedgerEntryType.CHARGE,
        )
    )
    assert charges.scalar_one() == 1


async def test_balance_stays_consistent_with_ledger(client, merchant, session):
    account, _, token = merchant
    for number in range(12):
        await client.get(qr_url(token, sum=1000 + number))

    await session.refresh(account)
    from_ledger = await calculate_balance_from_ledger(session, account.id)
    assert from_ledger == account.balance_kopecks


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


async def test_unknown_token_returns_placeholder_image(client):
    """В письме ошибка обязана выглядеть картинкой, а не битой иконкой."""
    response = await client.get(qr_url("totally-wrong-token"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers[ERROR_HEADER] == "invalid_token"
    assert response.content.startswith(PNG_MAGIC)


async def test_unknown_token_with_status_mode_returns_401(client):
    response = await client.get(qr_url("wrong", on_error="status"))
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


async def test_post_with_bad_token_returns_401(client):
    response = await client.post(
        "/v1/qr", headers={"Authorization": "Bearer nope"}, json={}
    )
    assert response.status_code == 401


async def test_revoked_token_stops_working(client, merchant, session, make_token):
    from nosbp.db.base import utcnow
    from nosbp.db.models import ApiToken

    account, _, token = merchant
    api_token = (
        await session.execute(select(ApiToken).where(ApiToken.account_id == account.id))
    ).scalar_one()
    api_token.revoked_at = utcnow()
    await session.commit()

    response = await client.get(qr_url(token, on_error="status"))
    assert response.status_code == 401


async def test_exhausted_overdraft_returns_readable_placeholder(
    client, merchant, session
):
    account, _, token = merchant
    account.balance_kopecks = -100
    account.overdraft_until = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=1
    )
    await session.commit()

    response = await client.get(qr_url(token, sum=1))
    assert response.headers[ERROR_HEADER] == "insufficient_funds"
    assert response.content.startswith(PNG_MAGIC)


async def test_too_long_purpose_is_rejected(client, merchant):
    _, _, token = merchant
    response = await client.get(
        qr_url(token, purpose="%D0%A4" * 210, on_error="status")
    )
    assert response.status_code in (422, 400)


async def test_no_charge_when_request_is_invalid(client, merchant, session):
    """За ошибочный запрос деньги списываться не должны."""
    account, _, token = merchant
    start = account.balance_kopecks

    await client.get(qr_url(token, org="missing"))

    await session.refresh(account)
    assert account.balance_kopecks == start


# ---------------------------------------------------------------------------
# Кэширование
# ---------------------------------------------------------------------------


async def test_response_carries_long_cache_headers(client, merchant, settings):
    """Почтовые прокси должны кэшировать картинку у себя, а не ходить к нам."""
    _, _, token = merchant
    response = await client.get(qr_url(token, sum=1234))

    max_age = settings.invoice_free_period_days * 24 * 3600
    assert response.headers["cache-control"] == f"public, max-age={max_age}"
    assert response.headers["etag"]


async def test_matching_etag_returns_304(client, merchant):
    _, _, token = merchant
    url = qr_url(token, sum=4321)

    first = await client.get(url)
    etag = first.headers["etag"]

    second = await client.get(url, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


async def test_error_response_is_not_cached(client):
    response = await client.get(qr_url("wrong-token"))
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------


async def test_organization_colour_is_applied(client, merchant, session, storage):
    from io import BytesIO

    from PIL import Image

    _, organization, token = merchant
    organization.qr_color = "#b22222"
    await session.commit()

    response = await client.get(qr_url(token, sum=1000))
    with Image.open(BytesIO(response.content)) as image:
        colors = {
            color for _, color in image.convert("RGB").getcolors(maxcolors=100000)
        }
    assert (178, 34, 34) in colors


async def test_logo_from_storage_lands_in_the_qr(client, merchant, session, storage):
    from io import BytesIO

    from PIL import Image

    _, organization, token = merchant

    logo = Image.new("RGBA", (150, 150), (12, 90, 200, 255))
    buffer = BytesIO()
    logo.save(buffer, format="PNG")
    await storage.put("logos/test.png", buffer.getvalue(), content_type="image/png")

    organization.logo_key = "logos/test.png"
    await session.commit()

    response = await client.get(qr_url(token, sum=1000))
    with Image.open(BytesIO(response.content)) as image:
        rgb = image.convert("RGB")
        centre = rgb.getpixel((rgb.width // 2, rgb.height // 2))
    assert centre == (12, 90, 200)


async def test_missing_logo_does_not_break_generation(client, merchant, session):
    """Ссылка на несуществующий файл не должна ронять генерацию."""
    _, organization, token = merchant
    organization.logo_key = "logos/does-not-exist.png"
    await session.commit()

    response = await client.get(qr_url(token, sum=1000))
    assert response.status_code == 200
    assert response.content.startswith(PNG_MAGIC)


# ---------------------------------------------------------------------------
# Персональные данные
# ---------------------------------------------------------------------------


async def test_payer_data_is_never_stored(client, merchant, session):
    """ФИО и телефон проходят в QR, но в базе их быть не должно."""
    _, _, token = merchant
    await client.get(
        qr_url(
            token,
            sum=1000,
            last_name="Иванов",
            first_name="Пётр",
            phone="%2B79001234567",
        )
    )

    invoice = (
        await session.execute(select(Invoice).where(Invoice.sum_kopecks == 1000))
    ).scalar_one()

    stored = " ".join(
        str(value) for value in invoice.__dict__.values() if value is not None
    )
    assert "Иванов" not in stored
    assert "Пётр" not in stored
    assert "79001234567" not in stored
