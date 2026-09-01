"""Тарификация: баланс, списания, овердрафт.

Правила, зафиксированные с заказчиком:

* один сгенерированный счёт стоит один рубль;
* повторное обращение к тому же счёту в течение 30 дней бесплатно
  (это обеспечивает :mod:`nosbp.payments.idempotency`, не этот модуль);
* при исчерпании баланса сервис работает в долг три дня — без ограничения
  по сумме, чтобы кассовый разрыв у заказчика не останавливал его продажи;
* овердрафт можно продлить из кабинета ещё на три дня.

Баланс — это журнал операций. Колонка ``Account.balance_kopecks`` только
кэширует его текущее значение и обновляется в той же транзакции.
"""

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.errors import (
    DailyLimitExceededError,
    InsufficientFundsError,
    NosbpError,
    ValidationError,
)
from nosbp.db.base import utcnow
from nosbp.db.models import Account, LedgerEntry, LedgerEntryType


@dataclass(frozen=True)
class SpendDecision:
    """Результат проверки, можно ли списать деньги."""

    allowed: bool
    starts_overdraft: bool = False
    reason: str = ""


class OverdraftNotActiveError(NosbpError):
    """Попытка продлить овердрафт, которого нет."""

    status_code = 409
    code = "overdraft_not_active"


class OverdraftExtensionLimitError(NosbpError):
    """Исчерпан лимит продлений овердрафта."""

    status_code = 409
    code = "overdraft_extension_limit"


class BillingService:
    """Операции с балансом одного аккаунта."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # ------------------------------------------------------------------
    # Блокировка
    # ------------------------------------------------------------------

    async def lock_account(self, account_id: uuid.UUID) -> Account:
        """Загружает аккаунт с блокировкой строки до конца транзакции.

        Блокировка сериализует денежные операции по одному аккаунту:
        два одновременных запроса не смогут дважды прочитать один и тот же
        баланс и списать с него по рублю, оставив итог неверным.

        На горячем пути (повтор уже существующего счёта) блокировка
        не берётся вовсе — там деньги не двигаются.
        """
        result = await self._session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        )
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Списания и пополнения
    # ------------------------------------------------------------------

    async def charge_for_invoice(
        self,
        account: Account,
        *,
        invoice_id: uuid.UUID,
        amount_kopecks: int | None = None,
        comment: str = "",
    ) -> LedgerEntry:
        """Списывает стоимость одного счёта.

        Вызывается только когда аккаунт уже заблокирован через
        :meth:`lock_account`.

        :raises InsufficientFundsError: баланс исчерпан и овердрафт закончился.
        :raises DailyLimitExceededError: превышен суточный потолок списаний.
        """
        amount = (
            self._settings.invoice_price_kopecks
            if amount_kopecks is None
            else amount_kopecks
        )
        now = utcnow()

        await self._ensure_daily_limit(account, amount, now)
        decision = self._evaluate_spend(account, amount, now)
        if not decision.allowed:
            raise InsufficientFundsError(decision.reason)

        if decision.starts_overdraft:
            account.overdraft_until = now + datetime.timedelta(
                days=self._settings.overdraft_days
            )
            account.overdraft_extensions_used = 0

        return self._append_entry(
            account,
            entry_type=LedgerEntryType.CHARGE,
            amount_kopecks=-amount,
            invoice_id=invoice_id,
            comment=comment,
        )

    async def topup(
        self, account: Account, amount_kopecks: int, *, comment: str = ""
    ) -> LedgerEntry:
        """Пополняет баланс.

        :raises ValidationError: если сумма меньше минимальной.
        """
        if amount_kopecks < self._settings.min_topup_kopecks:
            minimum = self._settings.min_topup_kopecks / 100
            raise ValidationError(f"Минимальная сумма пополнения — {minimum:.0f} ₽.")
        return self._append_entry(
            account,
            entry_type=LedgerEntryType.TOPUP,
            amount_kopecks=amount_kopecks,
            comment=comment,
        )

    async def refund(
        self,
        account: Account,
        amount_kopecks: int,
        *,
        comment: str,
        invoice_id: uuid.UUID | None = None,
    ) -> LedgerEntry:
        """Возвращает деньги на баланс.

        Отдельный тип операции, а не правка баланса: возвраты должны быть
        видны в выписке — в том числе те, что делаются заказчику навстречу,
        когда его ключ выжгли чужими запросами.
        """
        if amount_kopecks <= 0:
            raise ValidationError("Сумма возврата должна быть положительной.")
        return self._append_entry(
            account,
            entry_type=LedgerEntryType.REFUND,
            amount_kopecks=amount_kopecks,
            invoice_id=invoice_id,
            comment=comment,
        )

    async def adjust(
        self, account: Account, amount_kopecks: int, *, comment: str
    ) -> LedgerEntry:
        """Ручная корректировка баланса оператором сервиса."""
        if amount_kopecks == 0:
            raise ValidationError("Корректировка на ноль не имеет смысла.")
        return self._append_entry(
            account,
            entry_type=LedgerEntryType.ADJUSTMENT,
            amount_kopecks=amount_kopecks,
            comment=comment,
        )

    # ------------------------------------------------------------------
    # Овердрафт
    # ------------------------------------------------------------------

    def extend_overdraft(self, account: Account) -> datetime.datetime:
        """Продлевает овердрафт по просьбе заказчика.

        :return: новая дата окончания овердрафта.
        :raises OverdraftNotActiveError: овердрафт не начинался.
        :raises OverdraftExtensionLimitError: продлевать больше нельзя.
        """
        if account.overdraft_until is None:
            raise OverdraftNotActiveError("Овердрафт не активен — продлевать нечего.")
        if account.overdraft_extensions_used >= self._settings.overdraft_max_extensions:
            raise OverdraftExtensionLimitError(
                "Лимит продлений исчерпан. Пополните баланс, чтобы продолжить работу."
            )
        account.overdraft_until += datetime.timedelta(
            days=self._settings.overdraft_extension_days
        )
        account.overdraft_extensions_used += 1
        return account.overdraft_until

    # ------------------------------------------------------------------
    # Внутренняя кухня
    # ------------------------------------------------------------------

    def _evaluate_spend(
        self, account: Account, amount: int, now: datetime.datetime
    ) -> SpendDecision:
        """Решает, разрешено ли списание."""
        if not account.is_active:
            return SpendDecision(allowed=False, reason="Аккаунт отключён.")

        if account.balance_kopecks - amount >= 0:
            return SpendDecision(allowed=True)

        # Денег не хватает — смотрим на овердрафт.
        if account.overdraft_until is None:
            return SpendDecision(allowed=True, starts_overdraft=True)

        if now <= account.overdraft_until:
            return SpendDecision(allowed=True)

        return SpendDecision(
            allowed=False,
            reason=(
                "Баланс исчерпан, срок работы в долг истёк "
                f"{account.overdraft_until:%d.%m.%Y}. Пополните баланс."
            ),
        )

    async def _ensure_daily_limit(
        self, account: Account, amount: int, now: datetime.datetime
    ) -> None:
        """Проверяет суточный потолок списаний.

        Потолок защищает от единственного реального сценария утечки ключа:
        злоумышленник не может украсть деньги, но может выжечь баланс,
        генерируя счета с разными параметрами.
        """
        limit = account.daily_charge_limit_kopecks
        if limit is None:
            return

        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self._session.execute(
            select(func.coalesce(func.sum(-LedgerEntry.amount_kopecks), 0)).where(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == LedgerEntryType.CHARGE,
                LedgerEntry.created_at >= day_start,
            )
        )
        spent_today = int(result.scalar_one())
        if spent_today + amount > limit:
            raise DailyLimitExceededError(
                f"Превышен суточный лимит списаний ({limit / 100:.0f} ₽). "
                "Проверьте, не используется ли ваш ключ посторонними."
            )

    def _append_entry(
        self,
        account: Account,
        *,
        entry_type: LedgerEntryType,
        amount_kopecks: int,
        invoice_id: uuid.UUID | None = None,
        comment: str = "",
    ) -> LedgerEntry:
        """Добавляет запись в журнал и обновляет кэш баланса."""
        account.balance_kopecks += amount_kopecks

        # Баланс вернулся в плюс — эпизод овердрафта закрыт.
        if account.balance_kopecks >= 0:
            account.overdraft_until = None
            account.overdraft_extensions_used = 0

        entry = LedgerEntry(
            account_id=account.id,
            invoice_id=invoice_id,
            entry_type=entry_type,
            amount_kopecks=amount_kopecks,
            balance_after_kopecks=account.balance_kopecks,
            comment=comment,
        )
        self._session.add(entry)
        return entry


async def calculate_balance_from_ledger(
    session: AsyncSession, account_id: uuid.UUID
) -> int:
    """Считает баланс суммированием журнала.

    Источник истины для сверки: колонка ``Account.balance_kopecks`` — лишь
    кэш, и тест проверяет, что она не разошлась с журналом.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_kopecks), 0)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return int(result.scalar_one())
