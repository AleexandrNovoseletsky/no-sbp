"""Модели базы данных.

Схема отражает решения, зафиксированные с заказчиком:

* аккаунт (учётная запись) отделён от организаций-получателей платежа —
  у одного заказчика их может быть сколько угодно;
* ключи API хранятся только хэшем;
* персональные данные плательщика не хранятся вообще, они участвуют
  лишь в вычислении ключа идемпотентности;
* баланс — это журнал операций, а не редактируемое поле.
"""

import datetime
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nosbp.db.base import Base, created_at_column, utcnow, uuid_pk


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


class Account(Base):
    """Учётная запись заказчика — того, кто платит за сервис."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    display_name: Mapped[str] = mapped_column(String(160))

    balance_kopecks: Mapped[int] = mapped_column(BigInteger, default=0)
    """Кэш текущего баланса.

    Источник истины — таблица ``ledger_entries``; эта колонка обновляется
    в той же транзакции и существует только чтобы не суммировать журнал
    на каждом запросе. Тест ``test_billing`` проверяет, что они совпадают.
    """

    overdraft_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """До какого момента разрешено уходить в минус.

    Проставляется автоматически при первом уходе баланса ниже нуля
    и сбрасывается, как только баланс снова становится неотрицательным.
    """

    overdraft_extensions_used: Mapped[int] = mapped_column(Integer, default=0)
    """Сколько раз заказчик продлевал овердрафт в текущем эпизоде."""

    daily_charge_limit_kopecks: Mapped[int | None] = mapped_column(
        BigInteger, default=None
    )
    """Потолок списаний за календарные сутки. None — без ограничения."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = created_at_column()

    organizations: Mapped[list["Organization"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    tokens: Mapped[list["ApiToken"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account {self.email} balance={self.balance_kopecks}>"


class Organization(Base):
    """Получатель платежа — реквизиты, которые попадают в QR-код.

    Реквизиты статичны: они проверяются один раз при сохранении и дальше
    просто читаются. Благодаря этому горячий путь генерации QR не тратит
    время на валидацию банковских реквизитов.
    """

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("account_id", "alias", name="uq_organizations_account_alias"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    alias: Mapped[str] = mapped_column(String(64))
    """Короткое имя для параметра запроса (``?org=main``).

    Используется вместо UUID, чтобы ссылка в шаблоне CRM оставалась читаемой
    и не раскрывала внутренние идентификаторы.
    """

    # ---- Реквизиты по ГОСТ Р 56042-2014 --------------------------------
    name: Mapped[str] = mapped_column(String(160))
    personal_acc: Mapped[str] = mapped_column(String(20))
    bank_name: Mapped[str] = mapped_column(String(45))
    bic: Mapped[str] = mapped_column(String(9))
    corresp_acc: Mapped[str] = mapped_column(String(20))
    payee_inn: Mapped[str] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9), default=None)

    # ---- Оформление ----------------------------------------------------
    logo_key: Mapped[str | None] = mapped_column(String(255), default=None)
    """Ключ файла логотипа в объектном хранилище."""

    qr_color: Mapped[str] = mapped_column(String(7), default="#000000")
    """Цвет модулей QR-кода в формате ``#RRGGBB``.

    Фон всегда остаётся белым: прозрачный или светлый фон на тёмной подложке
    делает код нечитаемым для сканера.
    """

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = created_at_column()

    account: Mapped["Account"] = relationship(back_populates="organizations")

    def __repr__(self) -> str:
        return f"<Organization {self.alias} {self.name}>"


class ApiToken(Base):
    """Ключ доступа к API.

    Токен передаётся в GET-параметре, потому что шаблонизаторы CRM умеют
    вставлять только адрес картинки. Риск такой схемы обсуждён и принят:
    единственное, что даёт утечка ключа — возможность выжечь баланс
    заказчика генерацией счетов с разными параметрами. От этого защищают
    суточный потолок списаний и мгновенный отзыв ключа.

    В базе лежит только хэш: даже с полным доступом к дампу восстановить
    рабочий токен нельзя.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))
    """Первые символы токена открытым текстом — чтобы заказчик узнавал ключ
    в списке, не видя его целиком."""

    label: Mapped[str] = mapped_column(String(80), default="")
    """Человеческое имя: «RetailCRM», «Сайт», «1С»."""

    last_used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime.datetime] = created_at_column()

    account: Mapped["Account"] = relationship(back_populates="tokens")

    @property
    def is_usable(self) -> bool:
        """Ключ не отозван и может использоваться."""
        return self.revoked_at is None

    def __repr__(self) -> str:
        return f"<ApiToken {self.prefix}… {self.label}>"


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

    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """SHA-256 от нормализованных параметров запроса.

    В него входят и персональные данные плательщика — но только в виде
    вклада в хэш. Сами значения не сохраняются нигде.
    """

    public_token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    """Короткий идентификатор для ссылок вида ``/pay/{token}``."""

    sum_kopecks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    purpose: Mapped[str | None] = mapped_column(Text, default=None)

    charge_count: Mapped[int] = mapped_column(Integer, default=0)
    """Сколько раз за этот счёт списывали деньги.

    Больше единицы означает, что заказчик вернулся к тому же счёту
    после истечения бесплатного периода.
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
    __table_args__ = (CheckConstraint("amount_kopecks <> 0", name="amount_not_zero"),)

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
        Enum(LedgerEntryType, native_enum=False, length=16, validate_strings=True)
    )
    amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    """Сумма со знаком: пополнение положительное, списание отрицательное."""

    balance_after_kopecks: Mapped[int] = mapped_column(BigInteger)
    """Баланс после операции — чтобы выписка читалась без пересчёта."""

    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = created_at_column()

    def __repr__(self) -> str:
        return f"<LedgerEntry {self.entry_type} {self.amount_kopecks}>"
