"""Тарификация: баланс, списания, овердрафт.

Правила задаются настройками, а не зашиты в код:

* стоимость одного счёта — :attr:`Settings.invoice_price_kopecks`;
* длительность бесплатного периода — :attr:`Settings.invoice_free_period_days`
  (её обеспечивает :mod:`nosbp.invoices.service`, не этот модуль);
* сколько дней сервис работает в долг после исчерпания баланса —
  :attr:`Settings.overdraft_days`, ограничения по сумме при этом нет:
  кассовый разрыв у заказчика не должен останавливать его продажи;
* продление овердрафта — :attr:`Settings.overdraft_extension_days`
  и :attr:`Settings.overdraft_max_extensions`.

Баланс — это журнал операций. Колонка ``Account.balance_kopecks`` только
кэширует его текущее значение и обновляется в той же транзакции.
"""

import datetime
import uuid
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.errors import (
    AccountDisabledError,
    DailyLimitExceededError,
    InsufficientFundsError,
    OverdraftExtensionLimitError,
    OverdraftNotActiveError,
    ValidationError,
)
from nosbp.core.money import format_roubles
from nosbp.db.base import utcnow
from nosbp.db.models import Account, LedgerEntry, LedgerEntryType

DATE_FORMAT: Final[str] = "%d.%m.%Y"
"""Формат даты в сообщениях заказчику."""


def day_start(now: datetime.datetime, timezone_name: str) -> datetime.datetime:
    """Начало текущих суток в заданном часовом поясе, выраженное в UTC.

    Считать сутки по UTC нельзя: для московского заказчика суточный лимит
    сбрасывался бы в три часа ночи, а для тех, кто восточнее, — посреди
    рабочего дня.

    :param now: момент времени с часовым поясом.
    :param timezone_name: имя пояса из базы IANA, например ``Europe/Moscow``.
    """
    local_now = now.astimezone(ZoneInfo(timezone_name))
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(datetime.UTC)


@dataclass(frozen=True)
class SpendDecision:
    """Результат проверки, можно ли списать деньги."""

    allowed: bool
    starts_overdraft: bool = False
    reason: str = ""


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
        баланс и списать с него, оставив итог неверным.

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
        invoice_id: uuid.UUID | None,
        amount_kopecks: int | None = None,
        comment: str = "",
    ) -> LedgerEntry | None:
        """Списывает стоимость одного счёта.

        Вызывается только когда аккаунт уже заблокирован через
        :meth:`lock_account`.

        :param invoice_id: счёт, за который списываем. None допустим только
            в тестах и при служебных списаниях без привязки к счёту.
        :param amount_kopecks: сумма списания. По умолчанию берётся
            из настроек.
        :return: запись журнала или None, если аккаунт обслуживается
            без списаний.
        :raises AccountDisabledError: аккаунт отключён оператором.
        :raises InsufficientFundsError: баланс исчерпан и овердрафт закончился.
        :raises DailyLimitExceededError: превышен суточный потолок списаний.
        """
        if not account.is_active:
            raise AccountDisabledError(
                "Аккаунт отключён. Напишите в поддержку, чтобы разобраться."
            )

        # Безлимит: счёт создаётся и попадает в статистику, но денег
        # не стоит. Ни баланс, ни лимиты при этом не проверяются —
        # иначе нулевой баланс друга заблокировал бы генерацию.
        if account.is_unlimited:
            return None

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
        minimum = self._settings.min_topup_kopecks
        if amount_kopecks < minimum:
            raise ValidationError(
                "Минимальная сумма пополнения — "
                f"{format_roubles(minimum, fractional=False)}."
            )
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

        allowed_extensions = self._settings.overdraft_max_extensions
        if account.overdraft_extensions_used >= allowed_extensions:
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
        """Решает, разрешено ли списание.

        :raises AccountDisabledError: аккаунт отключён — это не про деньги,
            и пополнение баланса ничего не изменит.
        """
        if not account.is_active:
            raise AccountDisabledError(
                "Аккаунт отключён. Напишите в поддержку, чтобы разобраться."
            )

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
                f"{account.overdraft_until:{DATE_FORMAT}}. Пополните баланс."
            ),
        )

    async def _ensure_daily_limit(
        self, account: Account, amount: int, now: datetime.datetime
    ) -> None:
        """Проверяет суточный потолок списаний.

        Потолок защищает от единственного реального сценария утечки ключа:
        украсть деньги нельзя, но можно выжечь баланс, генерируя счета
        с разными параметрами.

        :raises DailyLimitExceededError: если потолок исчерпан.
        """
        limit = account.daily_charge_limit_kopecks
        if limit is None:
            return

        result = await self._session.execute(
            select(func.coalesce(func.sum(-LedgerEntry.amount_kopecks), 0)).where(
                LedgerEntry.account_id == account.id,
                LedgerEntry.entry_type == LedgerEntryType.CHARGE,
                LedgerEntry.created_at
                >= day_start(now, self._settings.billing_day_timezone),
            )
        )
        spent_today = int(result.scalar_one())

        if spent_today + amount > limit:
            raise DailyLimitExceededError(
                "Превышен суточный лимит списаний "
                f"({format_roubles(limit, fractional=False)}). "
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

    Журнал — источник истины; колонка ``Account.balance_kopecks`` лишь
    кэширует результат, и тест проверяет, что они не разошлись.
    """
    result = await session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_kopecks), 0)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return int(result.scalar_one())
