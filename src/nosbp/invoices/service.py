"""Поиск или создание счёта — оркестрация горячего пути.

Здесь сходятся тарификация и идемпотентность. Логика намеренно разделена
на два пути:

* **быстрый** — счёт с такими параметрами уже есть и бесплатный период
  не истёк. Одним запросом к базе увеличивается счётчик обращений, деньги
  не двигаются, блокировки не берутся. Сюда попадает большинство запросов:
  повторные открытия письма, предзагрузка картинок почтовым клиентом,
  превью в CRM;
* **медленный** — счёта нет или он «протух». Аккаунт блокируется,
  списывается стоимость счёта, счёт создаётся или продлевается.
"""

import datetime
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.billing.service import BillingService
from nosbp.core.config import Settings
from nosbp.db.base import utcnow
from nosbp.db.models import Account, Invoice, Organization
from nosbp.payments.idempotency import build_idempotency_key
from nosbp.payments.schemas import PaymentRequest

PUBLIC_TOKEN_BYTES: Final = 9
"""Длина публичного идентификатора счёта: 12 символов после base64."""

COMMENT_NEW_INVOICE: Final[str] = "Генерация счёта"
COMMENT_RENEWED_INVOICE: Final[str] = (
    "Повторная генерация счёта после бесплатного периода"
)
"""Тексты отображаются в выписке заказчика и изменяются согласованно
с документацией."""


@dataclass(frozen=True)
class InvoiceResolution:
    """Чем закончился поиск счёта."""

    invoice: Invoice
    charged: bool
    """True — за этот запрос списаны деньги; False — попадание в бесплатный
    период, обращение ничего не стоило."""


class InvoiceService:
    """Выдаёт счёт по параметрам запроса, при необходимости создавая его."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._billing = BillingService(session, settings)

    async def resolve(
        self,
        *,
        account: Account,
        organization: Organization,
        payment: PaymentRequest,
    ) -> InvoiceResolution:
        """Находит существующий счёт или создаёт новый, списав деньги."""
        key = build_idempotency_key(organization_id=organization.id, payment=payment)
        now = utcnow()

        existing = await self._touch_active_invoice(key, now)
        if existing is not None:
            return InvoiceResolution(invoice=existing, charged=False)

        return await self._create_or_renew(
            account=account,
            organization=organization,
            payment=payment,
            key=key,
            now=now,
        )

    # ------------------------------------------------------------------
    # Быстрый путь
    # ------------------------------------------------------------------

    async def _touch_active_invoice(
        self, key: str, now: datetime.datetime
    ) -> Invoice | None:
        """Отмечает обращение к живому счёту одним запросом к базе.

        Проверка «счёт существует и не истёк» и увеличение счётчика
        обращений выполняются одним UPDATE ... RETURNING: между проверкой
        и записью не может вклиниться другой запрос.
        """
        statement = (
            update(Invoice)
            .where(Invoice.idempotency_key == key, Invoice.expires_at > now)
            .values(hit_count=Invoice.hit_count + 1, last_seen_at=now)
            .returning(Invoice)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Медленный путь
    # ------------------------------------------------------------------

    async def _create_or_renew(
        self,
        *,
        account: Account,
        organization: Organization,
        payment: PaymentRequest,
        key: str,
        now: datetime.datetime,
    ) -> InvoiceResolution:
        """Создаёт новый счёт или продлевает истёкший, списывая деньги."""
        # Блокировка аккаунта сериализует денежные операции и заодно
        # исключает гонку двух одновременных запросов с одинаковым ключом:
        # оба уйдут сюда, но второй дождётся первого и увидит готовый счёт.
        locked_account = await self._billing.lock_account(account.id)

        found = await self._session.execute(
            select(Invoice).where(Invoice.idempotency_key == key)
        )
        invoice = found.scalar_one_or_none()

        if invoice is not None and invoice.is_free_period_active(now):
            # Пока мы ждали блокировку, счёт создал параллельный запрос.
            invoice.hit_count += 1
            invoice.last_seen_at = now
            return InvoiceResolution(invoice=invoice, charged=False)

        expires_at = now + datetime.timedelta(
            days=self._settings.invoice_free_period_days
        )

        if invoice is None:
            invoice = await self._create(
                account_id=locked_account.id,
                organization_id=organization.id,
                payment=payment,
                key=key,
                now=now,
                expires_at=expires_at,
            )
            comment = COMMENT_NEW_INVOICE
        else:
            # Бесплатный период истёк — счёт тарифицируется заново.
            invoice.expires_at = expires_at
            comment = COMMENT_RENEWED_INVOICE

        invoice.charge_count += 1
        invoice.hit_count += 1
        invoice.last_seen_at = now

        await self._billing.charge_for_invoice(
            locked_account, invoice_id=invoice.id, comment=comment
        )
        return InvoiceResolution(invoice=invoice, charged=True)

    async def _create(
        self,
        *,
        account_id: uuid.UUID,
        organization_id: uuid.UUID,
        payment: PaymentRequest,
        key: str,
        now: datetime.datetime,
        expires_at: datetime.datetime,
    ) -> Invoice:
        """Создаёт запись счёта и сразу получает её идентификатор.

        Идентификатор нужен до коммита: на него ссылается запись журнала
        списания, которая пишется в той же транзакции.
        """
        invoice = Invoice(
            account_id=account_id,
            organization_id=organization_id,
            idempotency_key=key,
            public_token=secrets.token_urlsafe(PUBLIC_TOKEN_BYTES),
            sum_kopecks=payment.sum_kopecks,
            purpose=payment.purpose,
            charge_count=0,
            hit_count=0,
            expires_at=expires_at,
            last_seen_at=now,
        )
        self._session.add(invoice)
        await self._session.flush()
        return invoice


async def get_invoice_by_public_token(
    session: AsyncSession, public_token: str
) -> Invoice | None:
    """Находит счёт по публичному идентификатору из ссылки ``/pay/{token}``."""
    result = await session.execute(
        select(Invoice).where(Invoice.public_token == public_token)
    )
    return result.scalar_one_or_none()


async def list_recent_invoices(
    session: AsyncSession, account_id: uuid.UUID, *, limit: int
) -> list[tuple[Invoice, str]]:
    """Возвращает последние счета заказчика вместе с алиасом организации.

    История счетов не зависит от списаний и остаётся содержательной для
    аккаунтов, обслуживаемых без тарификации.

    :return: пары «счёт, алиас организации», свежие сверху.
    """
    result = await session.execute(
        select(Invoice, Organization.alias)
        .join(Organization, Organization.id == Invoice.organization_id)
        .where(Invoice.account_id == account_id)
        .order_by(Invoice.last_seen_at.desc())
        .limit(limit)
    )
    return [(invoice, alias) for invoice, alias in result.all()]
