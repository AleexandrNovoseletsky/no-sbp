"""Зависимости FastAPI для эндпоинтов генерации QR."""

import datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings, get_settings
from nosbp.core.errors import (
    AccountDisabledError,
    InvalidTokenError,
    OrganizationNotFoundError,
)
from nosbp.db.base import utcnow
from nosbp.db.models import Account, ApiToken, Organization
from nosbp.db.session import get_db
from nosbp.payments.schemas import PayeeRequisites
from nosbp.payments.tokens import hash_token
from nosbp.storage.logo_cache import LogoCache

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_logo_cache(request: Request) -> LogoCache:
    """Отдаёт кэш логотипов, созданный при старте приложения."""
    cache: LogoCache = request.app.state.logo_cache
    return cache


LogoCacheDep = Annotated[LogoCache, Depends(get_logo_cache)]


async def _mark_token_used(
    session: AsyncSession,
    api_token: ApiToken,
    now: datetime.datetime,
    throttle_seconds: int,
) -> None:
    """Отмечает использование ключа отдельным запросом к базе.

    Два соображения, из-за которых это не простое присваивание атрибута.

    Первое: отметка обновляется не чаще, чем раз в ``throttle_seconds``.
    Условие стоит прямо в WHERE, поэтому при частых запросах строка
    вообще не изменяется — и блокировка на неё не берётся.

    Второе, важнее: запрос выполняется здесь и сейчас, до всех остальных
    операций. Если бы отметка ставилась присваиванием, SQLAlchemy отправил
    бы её в базу при коммите, в порядке, который зависит от того, какие
    ещё объекты оказались в сессии. Один ключ общий для всех запросов
    заказчика, и разный порядок захвата строк «ключ» и «аккаунт» в разных
    транзакциях приводил к взаимной блокировке при одновременных запросах.
    Единый порядок — ключ, потом аккаунт, потом счёт — эту возможность
    убирает.
    """
    threshold = now - datetime.timedelta(seconds=throttle_seconds)
    await session.execute(
        update(ApiToken)
        .where(
            ApiToken.id == api_token.id,
            or_(
                ApiToken.last_used_at.is_(None),
                ApiToken.last_used_at < threshold,
            ),
        )
        .values(last_used_at=now)
    )


async def authenticate(
    session: AsyncSession, token: str, *, settings: Settings
) -> Account:
    """Находит аккаунт по токену.

    Сравнение идёт по хэшу: сам токен в базе не хранится.

    :raises InvalidTokenError: ключ не найден или отозван.
    :raises AccountDisabledError: аккаунт отключён оператором сервиса.
    """
    result = await session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(token))
    )
    api_token = result.scalar_one_or_none()

    if api_token is None or not api_token.is_usable:
        raise InvalidTokenError("Ключ не найден или отозван.")

    account = await session.get(Account, api_token.account_id)
    if account is None:
        raise InvalidTokenError("Ключ не найден или отозван.")
    if not account.is_active:
        raise AccountDisabledError(
            "Аккаунт отключён. Напишите в поддержку, чтобы разобраться."
        )

    # Отметка последнего использования нужна заказчику в кабинете:
    # по ней видно, какой из ключей ещё работает, а какой можно отозвать.
    await _mark_token_used(
        session, api_token, utcnow(), settings.token_last_used_throttle_seconds
    )
    return account


def requisites_of(organization: Organization) -> PayeeRequisites:
    """Собирает реквизиты получателя из модели организации.

    Контрольные разряды проверены при сохранении организации, поэтому
    схема здесь — типизированный контракт, а не повторная валидация.
    """
    return PayeeRequisites(
        name=organization.name,
        personal_acc=organization.personal_acc,
        bank_name=organization.bank_name,
        bic=organization.bic,
        corresp_acc=organization.corresp_acc,
        payee_inn=organization.payee_inn,
        kpp=organization.kpp,
    )


async def resolve_organization(
    session: AsyncSession, account: Account, alias: str | None
) -> Organization:
    """Выбирает организацию-получателя платежа.

    Если алиас не передан, берётся организация по умолчанию — так заказчику
    с единственным юрлицом не приходится указывать её в каждом запросе.

    :raises OrganizationNotFoundError: подходящей организации нет.
    """
    query = select(Organization).where(Organization.account_id == account.id)
    if alias is not None:
        query = query.where(Organization.alias == alias)
    else:
        query = query.where(Organization.is_default.is_(True))

    result = await session.execute(query)
    organization = result.scalars().first()

    if organization is not None:
        return organization

    if alias is not None:
        raise OrganizationNotFoundError(
            f"Организация «{alias}» не найдена. Проверьте параметр org."
        )
    raise OrganizationNotFoundError(
        "У аккаунта нет организации по умолчанию. Укажите параметр org "
        "или отметьте одну из организаций основной."
    )
