"""Модели заказчика: учётная запись, организации, ключи, приглашения."""

import datetime
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nosbp.core.constants import (
    ACCOUNT_LENGTH,
    ALIAS_MAX_LENGTH,
    BANK_NAME_MAX_LENGTH,
    BIC_LENGTH,
    DEFAULT_ACQUIRING_FEE_BPS,
    DEFAULT_QR_COLOR,
    DISPLAY_NAME_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    INN_LENGTHS,
    KPP_LENGTH,
    NAME_MAX_LENGTH,
    PASSWORD_HASH_MAX_LENGTH,
    QR_COLOR_LENGTH,
    SHA256_HEX_LENGTH,
    STORAGE_KEY_MAX_LENGTH,
    TOKEN_LABEL_MAX_LENGTH,
    TOKEN_PREFIX_LENGTH,
)
from nosbp.db.base import Base, created_at_column, uuid_pk

MAX_INN_LENGTH = max(INN_LENGTHS)
"""Колонка рассчитана на самый длинный вариант — ИНН физлица и ИП."""

INVITE_PURPOSE_MAX_LENGTH = 16
"""Длина колонки с назначением ссылки."""


class InvitePurpose(enum.StrEnum):
    """Зачем выдана одноразовая ссылка.

    Назначение хранится рядом с токеном, чтобы ссылку нельзя было
    использовать не по адресу: письмо с подтверждением почты не должно
    открывать форму смены пароля.
    """

    PASSWORD = "password"
    """Установка или смена пароля: и первый вход, и восстановление."""

    EMAIL = "email"
    """Подтверждение адреса почты."""


class Account(Base):
    """Учётная запись заказчика — того, кто платит за сервис."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), unique=True, index=True
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(PASSWORD_HASH_MAX_LENGTH), default=None
    )
    display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_MAX_LENGTH))

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

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """До какого момента вход в кабинет заблокирован после неудачных попыток."""

    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    email_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """Когда владелец подтвердил, что почта его.

    Подтверждением считается переход по любой ссылке, отправленной на этот
    адрес: попасть в письмо может только тот, у кого есть доступ к ящику.
    Без подтверждения восстановление пароля работает, но письмо уходит
    на непроверенный адрес — поэтому подтверждение просим сразу.
    """

    is_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    """Обслуживание без списаний.

    Счета создаются и учитываются в статистике, но не тарифицируются;
    баланс и лимиты при этом не проверяются.
    """

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

    alias: Mapped[str] = mapped_column(String(ALIAS_MAX_LENGTH))
    """Короткое имя для параметра запроса (``?org=main``).

    Используется вместо UUID, чтобы ссылка в шаблоне CRM оставалась читаемой
    и не раскрывала внутренние идентификаторы.
    """

    # ---- Реквизиты по ГОСТ Р 56042-2014 --------------------------------
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH))
    personal_acc: Mapped[str] = mapped_column(String(ACCOUNT_LENGTH))
    bank_name: Mapped[str] = mapped_column(String(BANK_NAME_MAX_LENGTH))
    bic: Mapped[str] = mapped_column(String(BIC_LENGTH))
    corresp_acc: Mapped[str] = mapped_column(String(ACCOUNT_LENGTH))
    payee_inn: Mapped[str] = mapped_column(String(MAX_INN_LENGTH))
    kpp: Mapped[str | None] = mapped_column(String(KPP_LENGTH), default=None)

    # ---- Оформление ----------------------------------------------------
    logo_key: Mapped[str | None] = mapped_column(
        String(STORAGE_KEY_MAX_LENGTH), default=None
    )
    """Ключ файла логотипа в объектном хранилище."""

    qr_color: Mapped[str] = mapped_column(
        String(QR_COLOR_LENGTH), default=DEFAULT_QR_COLOR
    )
    """Цвет модулей QR-кода в формате ``#RRGGBB``.

    Фон всегда остаётся белым: прозрачный или светлый фон на тёмной подложке
    делает код нечитаемым для сканера.
    """

    # ---- Для подсчёта экономии -----------------------------------------
    acquiring_fee_bps: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_ACQUIRING_FEE_BPS
    )
    """Ставка эквайринга в базисных пунктах: 70 — это 0,7 %.

    У каждой организации своя: тарифы банков различаются, а у одного
    заказчика может быть и ООО с одной ставкой, и ИП с другой.
    """

    average_check_kopecks: Mapped[int | None] = mapped_column(BigInteger, default=None)
    """Средний чек — на случай счетов без указанной суммы.

    Если плательщик вводит сумму сам, посчитать оборот по счёту нельзя,
    и в оценку экономии подставляется это значение.
    """

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = created_at_column()

    account: Mapped["Account"] = relationship(back_populates="organizations")

    def __repr__(self) -> str:
        return f"<Organization {self.alias} {self.name}>"


class ApiToken(Base):
    """Ключ доступа к API.

    Токен передаётся в параметре запроса, поскольку шаблонизаторы CRM
    поддерживают только подстановку адреса изображения. Компрометация
    ключа позволяет расходовать баланс заказчика генерацией счетов
    с произвольными параметрами; ограничивают ущерб суточный лимит
    списаний и немедленный отзыв ключа.

    В базе хранится только хэш токена.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    token_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_LENGTH), unique=True, index=True
    )
    prefix: Mapped[str] = mapped_column(String(TOKEN_PREFIX_LENGTH))
    """Первые символы токена открытым текстом — чтобы заказчик узнавал ключ
    в списке, не видя его целиком."""

    label: Mapped[str] = mapped_column(String(TOKEN_LABEL_MAX_LENGTH), default="")
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


class AccountInvite(Base):
    """Одноразовая ссылка, отправленная заказчику на почту.

    Один механизм закрывает три случая: первый вход в аккаунт, заведённый
    оператором, восстановление забытого пароля и подтверждение адреса.
    Различает их поле :attr:`purpose`.
    """

    __tablename__ = "account_invites"

    id: Mapped[uuid.UUID] = uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    token_hash: Mapped[str] = mapped_column(
        String(SHA256_HEX_LENGTH), unique=True, index=True
    )
    """В базе только хэш: из дампа рабочую ссылку не восстановить."""

    purpose: Mapped[InvitePurpose] = mapped_column(
        # native_enum=False — значение хранится строкой с CHECK: добавить
        # новое назначение можно обычной миграцией, без ALTER TYPE.
        Enum(
            InvitePurpose,
            native_enum=False,
            length=INVITE_PURPOSE_MAX_LENGTH,
            validate_strings=True,
        ),
        default=InvitePurpose.PASSWORD,
    )

    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    """Ссылка одноразовая: повторное использование не проходит."""

    created_at: Mapped[datetime.datetime] = created_at_column()

    def is_usable(self, now: datetime.datetime) -> bool:
        """Ссылка ещё действует и не была использована."""
        return self.used_at is None and now < self.expires_at

    def __repr__(self) -> str:
        return f"<AccountInvite {self.purpose} account={self.account_id}>"
