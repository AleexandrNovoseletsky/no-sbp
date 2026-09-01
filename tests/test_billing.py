"""Тесты тарификации: списания, овердрафт, суточный лимит, журнал."""

import datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.billing.service import BillingService, calculate_balance_from_ledger
from nosbp.core.config import Settings
from nosbp.core.errors import (
    AccountDisabledError,
    DailyLimitExceededError,
    InsufficientFundsError,
    OverdraftExtensionLimitError,
    OverdraftNotActiveError,
    ValidationError,
)
from nosbp.db.models import Account, LedgerEntry, LedgerEntryType


async def charge_n(
    session: AsyncSession, settings: Settings, account: Account, times: int
) -> None:
    """Списывает стоимость нескольких счетов подряд."""
    billing = BillingService(session, settings)
    for _ in range(times):
        await billing.charge_for_invoice(account, invoice_id=None, comment="тест")
    await session.commit()


# ---------------------------------------------------------------------------
# Списания
# ---------------------------------------------------------------------------


async def test_one_invoice_costs_one_rouble(session, settings, make_account):
    account = await make_account(balance_rubles=10)
    await charge_n(session, settings, account, 1)
    assert account.balance_kopecks == 10 * 100 - 100


async def test_hundred_invoices_cost_hundred_roubles(session, settings, make_account):
    account = await make_account(balance_rubles=200)
    await charge_n(session, settings, account, 100)
    assert account.balance_kopecks == 200 * 100 - 100 * 100


async def test_ledger_matches_cached_balance(session, settings, make_account):
    """Колонка баланса — только кэш; журнал обязан давать то же число."""
    account = await make_account(balance_rubles=50)
    await charge_n(session, settings, account, 17)
    from_ledger = await calculate_balance_from_ledger(session, account.id)
    assert from_ledger == account.balance_kopecks


async def test_every_charge_leaves_a_ledger_entry(session, settings, make_account):
    account = await make_account(balance_rubles=10)
    await charge_n(session, settings, account, 3)
    result = await session.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.account_id == account.id,
            LedgerEntry.entry_type == LedgerEntryType.CHARGE,
        )
    )
    assert result.scalar_one() == 3


async def test_ledger_records_running_balance(session, settings, make_account):
    """По выписке должно быть видно состояние после каждой операции."""
    account = await make_account(balance_rubles=10)
    await charge_n(session, settings, account, 2)
    result = await session.execute(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == account.id)
        .order_by(LedgerEntry.created_at, LedgerEntry.balance_after_kopecks.desc())
    )
    balances = [entry.balance_after_kopecks for entry in result.scalars()]
    assert balances == [1000, 900, 800]


# ---------------------------------------------------------------------------
# Пополнение
# ---------------------------------------------------------------------------


async def test_topup_below_minimum_is_rejected(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)
    with pytest.raises(ValidationError, match="Минимальная сумма пополнения"):
        await billing.topup(account, 999 * 100)


async def test_topup_at_minimum_is_accepted(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)
    await billing.topup(account, settings.min_topup_kopecks)
    assert account.balance_kopecks == settings.min_topup_kopecks


# ---------------------------------------------------------------------------
# Овердрафт
# ---------------------------------------------------------------------------


async def test_zero_balance_starts_overdraft_instead_of_refusing(
    session, settings, make_account
):
    """Кассовый разрыв у заказчика не должен останавливать его продажи."""
    account = await make_account(balance_rubles=1)
    # Баланса хватает ровно на один счёт, второй уже уходит в минус.
    await charge_n(session, settings, account, 2)
    assert account.balance_kopecks == -100
    assert account.overdraft_until is not None


async def test_overdraft_has_no_sum_limit(session, settings, make_account):
    """Три дня в долг — без потолка по сумме, так решено с заказчиком."""
    account = await make_account(balance_rubles=1, daily_limit_rubles=None)
    await charge_n(session, settings, account, 500)
    assert account.balance_kopecks == 100 - 500 * 100


async def test_expired_overdraft_blocks_charges(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)

    await billing.charge_for_invoice(account, invoice_id=None)
    # Отматываем срок овердрафта в прошлое.
    account.overdraft_until = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )

    with pytest.raises(InsufficientFundsError, match="Баланс исчерпан"):
        await billing.charge_for_invoice(account, invoice_id=None)


async def test_topup_closes_overdraft(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)
    await billing.charge_for_invoice(account, invoice_id=None)
    assert account.overdraft_until is not None

    await billing.topup(account, 1000 * 100)
    assert account.overdraft_until is None
    assert account.overdraft_extensions_used == 0


async def test_overdraft_can_be_extended_once(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)
    await billing.charge_for_invoice(account, invoice_id=None)

    before = account.overdraft_until
    assert before is not None
    after = billing.extend_overdraft(account)
    assert after - before == datetime.timedelta(days=settings.overdraft_extension_days)


async def test_second_extension_is_refused(session, settings, make_account):
    account = await make_account(balance_rubles=0)
    billing = BillingService(session, settings)
    await billing.charge_for_invoice(account, invoice_id=None)

    billing.extend_overdraft(account)
    with pytest.raises(OverdraftExtensionLimitError, match="Лимит продлений"):
        billing.extend_overdraft(account)


async def test_extending_without_overdraft_is_an_error(session, settings, make_account):
    account = await make_account(balance_rubles=100)
    with pytest.raises(OverdraftNotActiveError):
        BillingService(session, settings).extend_overdraft(account)


# ---------------------------------------------------------------------------
# Суточный лимит
# ---------------------------------------------------------------------------


async def test_daily_limit_stops_balance_burning(session, settings, make_account):
    """Утечка ключа не должна стоить заказчику весь баланс."""
    account = await make_account(balance_rubles=1000, daily_limit_rubles=5)
    await charge_n(session, settings, account, 5)

    billing = BillingService(session, settings)
    with pytest.raises(DailyLimitExceededError, match="суточный лимит"):
        await billing.charge_for_invoice(account, invoice_id=None)


async def test_account_without_daily_limit_is_not_restricted(
    session, settings, make_account
):
    account = await make_account(balance_rubles=1000, daily_limit_rubles=None)
    await charge_n(session, settings, account, 300)
    assert account.balance_kopecks == 1000 * 100 - 300 * 100


# ---------------------------------------------------------------------------
# Возвраты и отключение
# ---------------------------------------------------------------------------


async def test_refund_is_a_separate_ledger_entry(session, settings, make_account):
    """Возврат должен быть виден в выписке, а не спрятан в правке баланса."""
    account = await make_account(balance_rubles=10)
    await charge_n(session, settings, account, 5)

    billing = BillingService(session, settings)
    await billing.refund(account, 500, comment="Ключ выжгли посторонние")
    await session.commit()

    result = await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.account_id == account.id,
            LedgerEntry.entry_type == LedgerEntryType.REFUND,
        )
    )
    entry = result.scalar_one()
    assert entry.amount_kopecks == 500
    assert entry.comment == "Ключ выжгли посторонние"
    assert account.balance_kopecks == 1000 - 500 + 500


async def test_disabled_account_cannot_spend(session, settings, make_account):
    """Отключённый аккаунт — это не про деньги.

    Ошибка должна отличаться от нехватки средств: пополнение баланса
    ничего не изменит, и заказчику важно это понимать.
    """
    account = await make_account(balance_rubles=100)
    account.is_active = False
    with pytest.raises(AccountDisabledError, match="отключён"):
        await BillingService(session, settings).charge_for_invoice(
            account, invoice_id=None
        )


async def test_zero_amount_adjustment_is_rejected(session, settings, make_account):
    account = await make_account(balance_rubles=10)
    with pytest.raises(ValidationError):
        await BillingService(session, settings).adjust(account, 0, comment="x")


async def test_lock_account_returns_same_row(session, settings, make_account):
    account = await make_account(balance_rubles=10)
    locked = await BillingService(session, settings).lock_account(account.id)
    assert locked.id == account.id
    assert isinstance(locked.id, uuid.UUID)
