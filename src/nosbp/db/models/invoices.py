"""Модели тарификации: счета и журнал движения средств."""

import datetime
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nosbp.core.constants import (
    INN_LENGTHS,
    PUBLIC_TOKEN_MAX_LENGTH,
    SHA256_HEX_LENGTH,
)
from nosbp.db.base import Base, created_at_column, utcnow, uuid_pk

MAX_INN_LENGTH = max(INN_LENGTHS)
"""Колонка рассчитана на самый длинный вариант — ИНН физлица и ИП."""

ENTRY_TYPE_MAX_LENGTH = 16
"""Длина колонки под название типа операции."""


class LedgerEntryType(enum.StrEnum):
    """Тип операции в журнале движения средств."""

    TOPUP = "topup"
    """Пополнение баланса заказчиком."""

    CHARGE = "charge"
    """Списание за сгенерированный счёт."""

    REFUND = "refund"
    """Возврат — например, когда чужой ключ выжег баланс."""

    ADJUSTMENT = "adjustment"
    """Ручная корректировка оператором сервиса."""


class Invoice(Base):
    """Счёт — уникальный набор параметров, за который списан рубль.

    Ключевая идея тарификации: платит заказчик не за обращение к API,
    а за счёт. Повторный запрос с теми же параметрами в течение
    ``invoice_free_period_days`` бесплатен — иначе предзагрузка картинок
    почтовыми клиентами тарифицировалась бы как новые счета.

    Сама картинка нигде не хранится: повторный запрос приносит те же
    параметры, поэтому QR просто рисуется заново. Это дешевле, чем держать
    десятки тысяч файлов в сутки.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(SHA256_HEX_LENGTH), unique=True, index=True
    )
    """SHA-256 от нормализованных параметров запроса.

    В него входят и персональные данные плательщика — но только в виде
    вклада в хэш. Сами значения не сохраняются нигде.
    """

    public_token: Mapped[str] = mapped_column(
        String(PUBLIC_TOKEN_MAX_LENGTH), unique=True, index=True
    )
    """Короткий идентификатор для ссылок вида ``/pay/{token}``."""

    sum_kopecks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    purpose: Mapped[str | None] = mapped_column(Text, default=None)

    charge_count: Mapped[int] = mapped_column(Integer, default=0)
    """Сколько раз счёт создавался или продлевался как тарифицируемое событие.

    Значение больше единицы означает возврат к тому же счёту после
    истечения бесплатного периода. У аккаунтов, обслуживаемых без
    списаний, счётчик растёт так же, но деньги не списываются.
    """

    hit_count: Mapped[int] = mapped_column(BigInteger, default=0)
    """Сколько всего было обращений, включая бесплатные."""

    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    """До этого момента повторные обращения бесплатны."""

    created_at: Mapped[datetime.datetime] = created_at_column()
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    def is_free_period_active(self, now: datetime.datetime) -> bool:
        """Действует ли ещё бесплатный период для этого счёта."""
        return now < self.expires_at

    def __repr__(self) -> str:
        return f"<Invoice {self.public_token} sum={self.sum_kopecks}>"


class LedgerEntry(Base):
    """Запись журнала движения средств.

    Баланс никогда не правится «руками»: любое изменение — это новая
    строка журнала. Когда заказчик спросит, куда делись деньги, ответом
    будет выписка с датами, а не одно число.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_kopecks <> 0", name="amount_not_zero"),
        # Под запрос суточного лимита: он выполняется перед каждым
        # списанием и без составного индекса вырождается в перебор
        # всех операций аккаунта за всё время.
        Index(
            "ix_ledger_entries_account_type_created",
            "account_id",
            "entry_type",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), default=None, index=True
    )

    entry_type: Mapped[LedgerEntryType] = mapped_column(
        # native_enum=False — тип хранится как VARCHAR с CHECK, а не как
        # отдельный тип PostgreSQL: добавить новое значение потом можно
        # обычной миграцией, без ALTER TYPE.
        Enum(
            LedgerEntryType,
            native_enum=False,
            length=ENTRY_TYPE_MAX_LENGTH,
            validate_strings=True,
        )
    )
    amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    """Сумма со знаком: пополнение положительное, списание отрицательное."""

    balance_after_kopecks: Mapped[int] = mapped_column(BigInteger)
    """Баланс после операции — чтобы выписка читалась без пересчёта."""

    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = created_at_column()

    def __repr__(self) -> str:
        return f"<LedgerEntry {self.entry_type} {self.amount_kopecks}>"
