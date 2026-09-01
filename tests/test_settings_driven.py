"""Тесты того, что значения берутся из настроек, а не зашиты в код.

Ради этого модуля стоит держать все параметры тарификации в одном месте:
если завтра счёт будет стоить не рубль, а два, менять придётся одну
переменную окружения, и вот эти тесты это подтверждают.
"""

import datetime

import pytest

from nosbp.billing.service import BillingService, day_start
from nosbp.core.config import Settings, get_settings
from nosbp.core.errors import InsufficientFundsError, ValidationError
from nosbp.invoices.service import InvoiceService
from nosbp.payments.schemas import PaymentRequest


def settings_with(**overrides: object) -> Settings:
    """Настройки по умолчанию с точечными заменами."""
    return get_settings().model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Стоимость счёта
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price_kopecks", [0, 1, 100, 250, 10_000])
async def test_invoice_price_comes_from_settings(session, make_account, price_kopecks):
    account = await make_account(balance_rubles=1000)
    start = account.balance_kopecks

    billing = BillingService(
        session, settings_with(invoice_price_kopecks=price_kopecks)
    )
    await billing.charge_for_invoice(account, invoice_id=None)

    assert account.balance_kopecks == start - price_kopecks


async def test_price_change_affects_generation(
    session, make_account, make_organization
):
    """Через весь путь создания счёта, а не только через списание."""
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account)
    start = account.balance_kopecks

    service = InvoiceService(session, settings_with(invoice_price_kopecks=333))
    await service.resolve(
        account=account,
        organization=organization,
        payment=PaymentRequest(sum_kopecks=5000),
    )
    await session.commit()

    assert account.balance_kopecks == start - 333


# ---------------------------------------------------------------------------
# Бесплатный период
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("free_days", [1, 7, 30, 365])
async def test_free_period_length_comes_from_settings(
    session, make_account, make_organization, free_days
):
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account)

    service = InvoiceService(session, settings_with(invoice_free_period_days=free_days))
    resolution = await service.resolve(
        account=account,
        organization=organization,
        payment=PaymentRequest(sum_kopecks=1234),
    )
    await session.commit()

    lifetime = resolution.invoice.expires_at - resolution.invoice.created_at
    assert abs(lifetime - datetime.timedelta(days=free_days)) < datetime.timedelta(
        seconds=5
    )


# ---------------------------------------------------------------------------
# Овердрафт
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overdraft_days", [0, 1, 3, 14])
async def test_overdraft_length_comes_from_settings(
    session, make_account, overdraft_days
):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings_with(overdraft_days=overdraft_days))

    before = datetime.datetime.now(datetime.UTC)
    await billing.charge_for_invoice(account, invoice_id=None)

    assert account.overdraft_until is not None
    granted = account.overdraft_until - before
    assert abs(granted - datetime.timedelta(days=overdraft_days)) < datetime.timedelta(
        seconds=5
    )


async def test_zero_overdraft_days_blocks_immediately(session, make_account):
    """Овердрафт можно отключить настройкой, не трогая код."""
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings_with(overdraft_days=0))

    # Первое списание открывает нулевой овердрафт и проходит...
    await billing.charge_for_invoice(account, invoice_id=None)
    # ...а следующее упирается в уже истёкший срок.
    with pytest.raises(InsufficientFundsError):
        await billing.charge_for_invoice(account, invoice_id=None)


@pytest.mark.parametrize("max_extensions", [0, 1, 3])
async def test_extension_count_comes_from_settings(
    session, make_account, max_extensions
):
    account = await make_account(balance_rubles=0)
    billing = BillingService(
        session, settings_with(overdraft_max_extensions=max_extensions)
    )
    await billing.charge_for_invoice(account, invoice_id=None)

    for _ in range(max_extensions):
        billing.extend_overdraft(account)

    assert account.overdraft_extensions_used == max_extensions


# ---------------------------------------------------------------------------
# Минимальное пополнение
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("minimum_kopecks", [0, 100, 100_000, 500_000])
async def test_minimum_topup_comes_from_settings(
    session, make_account, minimum_kopecks
):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings_with(min_topup_kopecks=minimum_kopecks))

    await billing.topup(account, minimum_kopecks or 1)

    if minimum_kopecks > 0:
        with pytest.raises(ValidationError, match="Минимальная сумма"):
            await billing.topup(account, minimum_kopecks - 1)


async def test_minimum_topup_message_shows_configured_value(session, make_account):
    """Текст ошибки собирается из настройки, а не написан числом в коде."""
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings_with(min_topup_kopecks=250_000))

    with pytest.raises(ValidationError, match="2500 ₽"):
        await billing.topup(account, 1)


# ---------------------------------------------------------------------------
# Часовой пояс суточного лимита
# ---------------------------------------------------------------------------


def test_daily_limit_day_starts_in_configured_timezone():
    """Сутки лимита начинаются по местному времени, а не по UTC.

    В UTC полночь наступает в три часа ночи по Москве — если бы лимит
    сбрасывался тогда, для заказчиков восточнее это была бы середина
    рабочего дня.
    """
    noon_utc = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)

    assert day_start(noon_utc, "Europe/Moscow") == datetime.datetime(
        2026, 8, 31, 21, 0, tzinfo=datetime.UTC
    )
    assert day_start(noon_utc, "UTC") == datetime.datetime(
        2026, 9, 1, 0, 0, tzinfo=datetime.UTC
    )
