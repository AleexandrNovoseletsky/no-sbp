"""Подсчёт экономии заказчика.

Смысл сервиса — заменить эквайринг переводом по реквизитам. Чтобы это
было видно цифрами, для каждой организации хранится её ставка эквайринга,
и по обороту считается, сколько банк взял бы комиссии.

Всё считается в целых копейках и базисных пунктах: ни одного числа
с плавающей точкой на пути к сумме, которую увидит заказчик.
"""

import datetime
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.constants import (
    BASIS_POINTS_PER_PERCENT,
    BASIS_POINTS_TOTAL,
    MAX_ACQUIRING_FEE_BPS,
)
from nosbp.db.models import (
    Account,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    Organization,
)


@dataclass(frozen=True)
class OrganizationStats:
    """Итоги по одной организации-получателю."""

    organization: Organization

    invoice_count: int
    """Уникальных операций: сколько разных счетов выставлено."""

    charge_count: int
    """Сколько раз счета тарифицировались.

    Больше числа счетов означает, что к каким-то возвращались после
    истечения бесплатного периода.
    """

    invoices_without_sum: int
    """Счета, где сумму вводит плательщик — в оборот они входят по
    среднему чеку организации, а без него не входят вовсе."""

    turnover_kopecks: int
    """Оборот, прошедший через выставленные счета."""

    charged_kopecks: int
    """Сколько заказчик заплатил нам за эти счета."""

    @property
    def acquiring_fee_bps(self) -> int:
        return self.organization.acquiring_fee_bps

    @property
    def acquiring_cost_kopecks(self) -> int:
        """Во что обошёлся бы тот же оборот через эквайринг."""
        return self.turnover_kopecks * self.acquiring_fee_bps // BASIS_POINTS_TOTAL

    @property
    def savings_kopecks(self) -> int:
        """Сэкономлено: комиссия эквайринга минус плата за сервис."""
        return self.acquiring_cost_kopecks - self.charged_kopecks

    @property
    def turnover_is_estimated(self) -> bool:
        """Часть оборота получена подстановкой среднего чека."""
        return self.invoices_without_sum > 0


@dataclass(frozen=True)
class AccountStats:
    """Итоги по заказчику целиком."""

    account: Account
    organizations: tuple[OrganizationStats, ...]

    @property
    def invoice_count(self) -> int:
        return sum(item.invoice_count for item in self.organizations)

    @property
    def charge_count(self) -> int:
        return sum(item.charge_count for item in self.organizations)

    @property
    def turnover_kopecks(self) -> int:
        return sum(item.turnover_kopecks for item in self.organizations)

    @property
    def charged_kopecks(self) -> int:
        return sum(item.charged_kopecks for item in self.organizations)

    @property
    def acquiring_cost_kopecks(self) -> int:
        return sum(item.acquiring_cost_kopecks for item in self.organizations)

    @property
    def savings_kopecks(self) -> int:
        return sum(item.savings_kopecks for item in self.organizations)


_EMPTY_COUNTS: Final[tuple[int, int, int, int]] = (0, 0, 0, 0)

InvoiceTotals = dict[uuid.UUID, tuple[int, int, int, int]]
ChargeTotals = dict[uuid.UUID, int]


async def collect_account_stats(
    session: AsyncSession,
    account: Account,
    *,
    since: datetime.datetime | None = None,
) -> AccountStats:
    """Собирает статистику по одному заказчику.

    :param since: считать только счета, созданные не раньше этого момента.
        None — за всё время.
    """
    collected = await collect_many_account_stats(session, [account], since=since)
    return collected[0]


async def collect_many_account_stats(
    session: AsyncSession,
    accounts: Sequence[Account],
    *,
    since: datetime.datetime | None = None,
) -> list[AccountStats]:
    """Собирает статистику сразу по нескольким заказчикам.

    Три запроса на весь список, а не три на каждого. Список заказчиков —
    главная страница панели, и считать её десятками запросов означало бы
    ждать секунду на сотне строк вместо десятка миллисекунд.
    """
    if not accounts:
        return []

    account_ids = [account.id for account in accounts]
    organizations = await _load_organizations(session, account_ids)
    invoice_totals = await _invoice_totals(session, account_ids, since)
    charge_totals = await _charge_totals(session, account_ids, since)

    return [
        AccountStats(
            account=account,
            organizations=tuple(
                _build_stats(
                    organization,
                    invoice_totals.get(organization.id, _EMPTY_COUNTS),
                    charge_totals.get(organization.id, 0),
                )
                for organization in organizations.get(account.id, ())
            ),
        )
        for account in accounts
    ]


def _build_stats(
    organization: Organization,
    totals: tuple[int, int, int, int],
    charged_kopecks: int,
) -> OrganizationStats:
    """Собирает итоги одной организации, дополняя оборот средним чеком."""
    invoice_count, charge_count, known_sum, without_sum = totals

    # Счета без суммы дают оборот только если задан средний чек.
    estimated = (organization.average_check_kopecks or 0) * without_sum

    return OrganizationStats(
        organization=organization,
        invoice_count=invoice_count,
        charge_count=charge_count,
        invoices_without_sum=without_sum,
        turnover_kopecks=known_sum + estimated,
        charged_kopecks=charged_kopecks,
    )


async def _load_organizations(
    session: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Organization]]:
    """Загружает организации всех заказчиков разом, в постоянном порядке."""
    result = await session.execute(
        select(Organization)
        .where(Organization.account_id.in_(account_ids))
        .order_by(Organization.is_default.desc(), Organization.alias)
    )
    grouped: dict[uuid.UUID, list[Organization]] = defaultdict(list)
    for organization in result.scalars():
        grouped[organization.account_id].append(organization)
    return grouped


async def _invoice_totals(
    session: AsyncSession,
    account_ids: Sequence[uuid.UUID],
    since: datetime.datetime | None,
) -> InvoiceTotals:
    """Считает по каждой организации: счета, тарификации, оборот, счета без суммы."""
    query = (
        select(
            Invoice.organization_id,
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.charge_count), 0),
            func.coalesce(func.sum(Invoice.sum_kopecks), 0),
            func.count(Invoice.id).filter(Invoice.sum_kopecks.is_(None)),
        )
        .where(Invoice.account_id.in_(account_ids))
        .group_by(Invoice.organization_id)
    )
    if since is not None:
        query = query.where(Invoice.created_at >= since)

    result = await session.execute(query)
    return {
        row[0]: (int(row[1]), int(row[2]), int(row[3]), int(row[4]))
        for row in result.all()
    }


async def _charge_totals(
    session: AsyncSession,
    account_ids: Sequence[uuid.UUID],
    since: datetime.datetime | None,
) -> ChargeTotals:
    """Считает, сколько списано за счета каждой организации.

    Журнал соединяется со счетами: сама запись списания не знает, к какой
    организации относится, эта связь есть только у счёта.
    """
    query = (
        select(
            Invoice.organization_id,
            func.coalesce(func.sum(-LedgerEntry.amount_kopecks), 0),
        )
        .join(Invoice, Invoice.id == LedgerEntry.invoice_id)
        .where(
            LedgerEntry.account_id.in_(account_ids),
            LedgerEntry.entry_type == LedgerEntryType.CHARGE,
        )
        .group_by(Invoice.organization_id)
    )
    if since is not None:
        query = query.where(LedgerEntry.created_at >= since)

    result = await session.execute(query)
    return {row[0]: int(row[1]) for row in result.all()}


def month_start(now: datetime.datetime) -> datetime.datetime:
    """Начало текущего календарного месяца — период по умолчанию."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def format_fee_percent(bps: int) -> str:
    """Показывает ставку в процентах: 70 базисных пунктов — это «0,7 %»."""
    whole, fraction = divmod(bps, 100)
    text = f"{whole},{fraction:02d}".rstrip("0").rstrip(",")
    return f"{text} %"


def parse_fee_percent(text: str) -> int:
    """Разбирает ставку эквайринга в процентах и переводит в базисные пункты.

    «0,7» или «0.7» превращается в 70. Дробнее сотой доли процента ставок
    не бывает, поэтому лишняя точность отвергается, а не округляется молча.

    :raises ValueError: если строка не похожа на ставку.
    """
    cleaned = text.strip().replace(" ", "").replace(",", ".")
    if not cleaned:
        raise ValueError("Ставка не указана.")
    try:
        percent = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"«{text}» не похоже на процент.") from exc

    bps = percent * BASIS_POINTS_PER_PERCENT
    if bps != bps.to_integral_value():
        raise ValueError("Ставка указывается с точностью до сотой доли процента.")
    if not 0 <= bps <= MAX_ACQUIRING_FEE_BPS:
        raise ValueError("Ставка должна быть от 0 до 100 %.")
    return int(bps)
