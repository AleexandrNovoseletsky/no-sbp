"""Тесты подсчёта экономии на комиссии эквайринга."""

import datetime

import pytest

from nosbp.admin.stats import (
    collect_account_stats,
    format_fee_percent,
    month_start,
    parse_fee_percent,
)
from nosbp.core.config import get_settings
from nosbp.invoices.service import InvoiceService
from nosbp.payments.schemas import PaymentRequest


async def generate(session, account, organization, *sums: int | None) -> None:
    """Создаёт по счёту на каждую сумму."""
    service = InvoiceService(session, get_settings())
    for value in sums:
        await service.resolve(
            account=account,
            organization=organization,
            payment=PaymentRequest(sum_kopecks=value, purpose=f"Заказ {value}"),
        )
    await session.commit()


# ---------------------------------------------------------------------------
# Форматирование ставки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bps", "text"),
    [(0, "0 %"), (1, "0,01 %"), (70, "0,7 %"), (100, "1 %"), (250, "2,5 %")],
)
def test_fee_is_shown_as_percent(bps: int, text: str):
    assert format_fee_percent(bps) == text


@pytest.mark.parametrize(
    ("text", "bps"), [("0,7", 70), ("0.7", 70), ("2,5", 250), ("1", 100), ("0", 0)]
)
def test_percent_is_parsed_to_basis_points(text: str, bps: int):
    assert parse_fee_percent(text) == bps


@pytest.mark.parametrize("bad", ["", "процент", "0,001", "101"])
def test_bad_fee_is_rejected(bad: str):
    with pytest.raises(ValueError):
        parse_fee_percent(bad)


def test_parse_and_format_round_trip():
    for bps in (0, 1, 70, 250, 1000, 10_000):
        assert parse_fee_percent(format_fee_percent(bps).replace(" %", "")) == bps


# ---------------------------------------------------------------------------
# Подсчёт
# ---------------------------------------------------------------------------


async def test_empty_account_has_no_stats(session, make_account):
    account = await make_account()
    stats = await collect_account_stats(session, account)
    assert stats.organizations == ()
    assert stats.invoice_count == 0
    assert stats.savings_kopecks == 0


async def test_unique_operations_are_counted(session, make_account, make_organization):
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account)

    await generate(session, account, organization, 100_00, 200_00, 300_00)

    stats = await collect_account_stats(session, account)
    assert stats.invoice_count == 3
    assert stats.charge_count == 3


async def test_repeated_request_does_not_add_an_operation(
    session, make_account, make_organization
):
    """Повторное открытие письма — не новая операция."""
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account)

    await generate(session, account, organization, 100_00)
    await generate(session, account, organization, 100_00)

    stats = await collect_account_stats(session, account)
    assert stats.invoice_count == 1
    assert stats.charged_kopecks == 100


async def test_savings_use_the_organization_fee(
    session, make_account, make_organization
):
    """Ваш же случай: чек 47 000 ₽ при ставке 0,7 % — это 329 ₽ комиссии."""
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account, acquiring_fee_bps=70)

    await generate(session, account, organization, 4_700_000)

    stats = await collect_account_stats(session, account)
    item = stats.organizations[0]

    assert item.turnover_kopecks == 4_700_000
    assert item.acquiring_cost_kopecks == 32_900
    assert item.charged_kopecks == 100
    assert item.savings_kopecks == 32_800


async def test_each_organization_keeps_its_own_fee(
    session, make_account, make_organization
):
    """У одного заказчика может быть ООО с одной ставкой и ИП с другой."""
    account = await make_account(balance_rubles=1000)
    company = await make_organization(account, alias="ooo", acquiring_fee_bps=70)
    entrepreneur = await make_organization(account, alias="ip", acquiring_fee_bps=250)

    await generate(session, account, company, 1_000_000)
    await generate(session, account, entrepreneur, 1_000_000)

    stats = await collect_account_stats(session, account)
    by_alias = {item.organization.alias: item for item in stats.organizations}

    assert by_alias["ooo"].acquiring_cost_kopecks == 7_000
    assert by_alias["ip"].acquiring_cost_kopecks == 25_000
    assert stats.acquiring_cost_kopecks == 32_000


async def test_invoices_without_sum_use_average_check(
    session, make_account, make_organization
):
    """Когда сумму вводит плательщик, оборот считается по среднему чеку."""
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(
        account, acquiring_fee_bps=100, average_check_kopecks=500_000
    )

    await generate(session, account, organization, None, 100_000)

    item = (await collect_account_stats(session, account)).organizations[0]
    assert item.invoices_without_sum == 1
    assert item.turnover_kopecks == 600_000
    assert item.turnover_is_estimated


async def test_without_average_check_such_invoices_are_skipped(
    session, make_account, make_organization
):
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(
        account, acquiring_fee_bps=100, average_check_kopecks=None
    )

    # Оба счёта без суммы, но с разным назначением — иначе это был бы
    # один и тот же счёт, и второе обращение оказалось бы бесплатным.
    service = InvoiceService(session, get_settings())
    for order in ("Заказ 1", "Заказ 2"):
        await service.resolve(
            account=account,
            organization=organization,
            payment=PaymentRequest(sum_kopecks=None, purpose=order),
        )
    await session.commit()

    item = (await collect_account_stats(session, account)).organizations[0]
    assert item.invoice_count == 2
    assert item.invoices_without_sum == 2
    assert item.turnover_kopecks == 0


async def test_unlimited_account_pays_nothing_but_still_counts(
    session, make_account, make_organization
):
    """Безлимит для своих: операции считаются, деньги не списываются."""
    account = await make_account(balance_rubles=0)
    account.is_unlimited = True
    await session.commit()

    organization = await make_organization(account, acquiring_fee_bps=70)
    await generate(session, account, organization, 4_700_000, 1_000_000)

    stats = await collect_account_stats(session, account)
    assert stats.invoice_count == 2
    assert stats.charged_kopecks == 0
    assert account.balance_kopecks == 0
    # Вся комиссия эквайринга целиком идёт в экономию.
    assert stats.savings_kopecks == stats.acquiring_cost_kopecks


async def test_period_filter(session, make_account, make_organization):
    account = await make_account(balance_rubles=1000)
    organization = await make_organization(account)

    await generate(session, account, organization, 100_000)

    all_time = await collect_account_stats(session, account, since=None)
    assert all_time.invoice_count == 1

    future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    nothing = await collect_account_stats(session, account, since=future)
    assert nothing.invoice_count == 0
    assert nothing.savings_kopecks == 0


def test_month_start_drops_time_and_day():
    moment = datetime.datetime(2026, 9, 17, 13, 45, 30, tzinfo=datetime.UTC)
    assert month_start(moment) == datetime.datetime(
        2026, 9, 1, 0, 0, tzinfo=datetime.UTC
    )


async def test_totals_add_up_across_organizations(
    session, make_account, make_organization
):
    account = await make_account(balance_rubles=1000)
    first = await make_organization(account, alias="a")
    second = await make_organization(account, alias="b")

    await generate(session, account, first, 100_000, 200_000)
    await generate(session, account, second, 300_000)

    stats = await collect_account_stats(session, account)
    assert stats.invoice_count == 3
    assert stats.turnover_kopecks == 600_000
    assert stats.charged_kopecks == 300
    assert stats.savings_kopecks == sum(
        item.savings_kopecks for item in stats.organizations
    )
