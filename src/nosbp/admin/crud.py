"""Операции над заказчиками и их организациями.

Один и тот же код используют и панель управления, и консольная утилита:
проверки реквизитов и правила «организация по умолчанию только одна»
не должны существовать в двух экземплярах.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.errors import ValidationError
from nosbp.db.models import Account, ApiToken, Organization
from nosbp.payments.qr import validate_qr_color
from nosbp.payments.requisites import (
    validate_account,
    validate_bic,
    validate_inn,
    validate_kpp,
)
from nosbp.payments.tokens import generate_token, hash_token, token_prefix


@dataclass(frozen=True)
class RequisitesInput:
    """Реквизиты организации в том виде, в каком их ввёл человек."""

    alias: str
    name: str
    personal_acc: str
    bank_name: str
    bic: str
    corresp_acc: str
    payee_inn: str
    kpp: str | None = None
    qr_color: str = "#000000"
    acquiring_fee_bps: int = 70
    average_check_kopecks: int | None = None
    is_default: bool = False


# ---------------------------------------------------------------------------
# Заказчики
# ---------------------------------------------------------------------------


async def list_accounts(session: AsyncSession, *, search: str = "") -> list[Account]:
    """Возвращает заказчиков, при необходимости отфильтровав по подстроке."""
    query = select(Account).order_by(Account.created_at.desc())
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            func.lower(Account.email).like(pattern)
            | func.lower(Account.display_name).like(pattern)
        )
    result = await session.execute(query)
    return list(result.scalars())


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account:
    """Находит заказчика или сообщает, что его нет."""
    account = await session.get(Account, account_id)
    if account is None:
        raise ValidationError("Заказчик не найден.")
    return account


async def create_account(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    display_name: str,
    daily_limit_kopecks: int | None = None,
    is_unlimited: bool = False,
    use_default_limit: bool = True,
) -> Account:
    """Заводит нового заказчика.

    :param use_default_limit: подставить суточный лимит из настроек, если
        он не задан явно.
    :raises ValidationError: если адрес почты уже занят.
    """
    normalized = email.strip().lower()
    if not normalized:
        raise ValidationError("Укажите адрес почты.")

    existing = await session.execute(select(Account).where(Account.email == normalized))
    if existing.scalar_one_or_none() is not None:
        raise ValidationError(f"Заказчик с адресом {normalized} уже заведён.")

    limit = daily_limit_kopecks
    if limit is None and use_default_limit:
        limit = settings.default_daily_charge_limit_kopecks

    account = Account(
        email=normalized,
        display_name=display_name.strip(),
        daily_charge_limit_kopecks=limit,
        is_unlimited=is_unlimited,
    )
    session.add(account)
    await session.flush()
    return account


async def update_account(
    account: Account,
    *,
    display_name: str,
    is_active: bool,
    is_unlimited: bool,
    daily_limit_kopecks: int | None,
) -> Account:
    """Меняет карточку заказчика.

    Адрес почты не меняется: к нему привязаны ключи и переписка, а замена
    почты — это по сути другой заказчик.
    """
    if not display_name.strip():
        raise ValidationError("Название компании не может быть пустым.")

    account.display_name = display_name.strip()
    account.is_active = is_active
    account.is_unlimited = is_unlimited
    account.daily_charge_limit_kopecks = daily_limit_kopecks
    return account


async def delete_account(session: AsyncSession, account: Account) -> None:
    """Удаляет заказчика вместе с организациями, ключами и журналом."""
    await session.delete(account)


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


async def create_organization(
    session: AsyncSession, account: Account, data: RequisitesInput
) -> Organization:
    """Добавляет организацию-получателя после проверки реквизитов."""
    fields = _validated_fields(data)

    duplicate = await session.execute(
        select(Organization).where(
            Organization.account_id == account.id,
            Organization.alias == fields["alias"],
        )
    )
    if duplicate.scalar_one_or_none() is not None:
        raise ValidationError(
            f"У заказчика уже есть организация с коротким именем «{fields['alias']}»."
        )

    if data.is_default:
        await clear_default_organization(session, account.id)

    organization = Organization(account_id=account.id, **fields)
    session.add(organization)
    await session.flush()
    return organization


async def update_organization(
    session: AsyncSession, organization: Organization, data: RequisitesInput
) -> Organization:
    """Меняет реквизиты и настройки организации."""
    fields = _validated_fields(data)

    if fields["alias"] != organization.alias:
        duplicate = await session.execute(
            select(Organization).where(
                Organization.account_id == organization.account_id,
                Organization.alias == fields["alias"],
                Organization.id != organization.id,
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise ValidationError(
                f"Короткое имя «{fields['alias']}» уже занято у этого заказчика."
            )

    if data.is_default and not organization.is_default:
        await clear_default_organization(session, organization.account_id)

    for name, value in fields.items():
        setattr(organization, name, value)
    return organization


async def delete_organization(
    session: AsyncSession, organization: Organization
) -> None:
    """Удаляет организацию вместе с её счетами."""
    await session.delete(organization)


async def clear_default_organization(
    session: AsyncSession, account_id: uuid.UUID
) -> None:
    """Снимает признак «по умолчанию» с прежней организации.

    Организация по умолчанию может быть только одна: иначе запрос без
    параметра org выбирал бы её случайно.
    """
    result = await session.execute(
        select(Organization).where(
            Organization.account_id == account_id,
            Organization.is_default.is_(True),
        )
    )
    for organization in result.scalars():
        organization.is_default = False


def _validated_fields(data: RequisitesInput) -> dict[str, object]:
    """Проверяет ввод и возвращает готовый набор полей модели.

    Все проверки собраны в одном месте, чтобы панель и консоль не могли
    разойтись в том, что считается корректными реквизитами.
    """
    alias = data.alias.strip().lower()
    if not alias:
        raise ValidationError("Укажите короткое имя организации для параметра org.")
    if not data.name.strip():
        raise ValidationError("Укажите наименование получателя платежа.")
    if not data.bank_name.strip():
        raise ValidationError("Укажите наименование банка.")
    if not 0 <= data.acquiring_fee_bps <= 10_000:
        raise ValidationError("Ставка эквайринга должна быть от 0 до 100 %.")
    if data.average_check_kopecks is not None and data.average_check_kopecks < 0:
        raise ValidationError("Средний чек не может быть отрицательным.")

    bic = validate_bic(data.bic)
    return {
        "alias": alias,
        "name": data.name.strip(),
        "personal_acc": validate_account(data.personal_acc, bic),
        "bank_name": data.bank_name.strip(),
        "bic": bic,
        "corresp_acc": validate_account(data.corresp_acc, bic),
        "payee_inn": validate_inn(data.payee_inn),
        "kpp": validate_kpp(data.kpp),
        "qr_color": validate_qr_color(data.qr_color),
        "acquiring_fee_bps": data.acquiring_fee_bps,
        "average_check_kopecks": data.average_check_kopecks,
        "is_default": data.is_default,
    }


# ---------------------------------------------------------------------------
# Ключи API
# ---------------------------------------------------------------------------


async def issue_token(session: AsyncSession, account: Account, *, label: str) -> str:
    """Выпускает ключ и возвращает его открытым текстом.

    Возвращённое значение больше нигде не появится: в базе лежит хэш.
    """
    token = generate_token()
    session.add(
        ApiToken(
            account_id=account.id,
            token_hash=hash_token(token),
            prefix=token_prefix(token),
            label=label.strip(),
        )
    )
    await session.flush()
    return token


async def list_tokens(session: AsyncSession, account_id: uuid.UUID) -> list[ApiToken]:
    """Возвращает ключи заказчика, свежие сверху."""
    result = await session.execute(
        select(ApiToken)
        .where(ApiToken.account_id == account_id)
        .order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars())
