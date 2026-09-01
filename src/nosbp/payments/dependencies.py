"""Зависимости FastAPI для эндпоинтов генерации QR."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings, get_settings
from nosbp.core.errors import InvalidTokenError, OrganizationNotFoundError
from nosbp.db.base import utcnow
from nosbp.db.models import Account, ApiToken, Organization
from nosbp.db.session import get_db
from nosbp.payments.tokens import hash_token
from nosbp.storage.logo_cache import LogoCache

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_logo_cache(request: Request) -> LogoCache:
    """Отдаёт кэш логотипов, созданный при старте приложения."""
    cache: LogoCache = request.app.state.logo_cache
    return cache


LogoCacheDep = Annotated[LogoCache, Depends(get_logo_cache)]


async def authenticate(session: AsyncSession, token: str) -> Account:
    """Находит аккаунт по токену.

    Сравнение идёт по хэшу: сам токен в базе не хранится.

    :raises InvalidTokenError: токен не найден, отозван либо аккаунт отключён.
    """
    result = await session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(token))
    )
    api_token = result.scalar_one_or_none()

    if api_token is None or not api_token.is_usable:
        raise InvalidTokenError("Ключ не найден или отозван.")

    account = await session.get(Account, api_token.account_id)
    if account is None or not account.is_active:
        raise InvalidTokenError("Аккаунт отключён.")

    # Отметка последнего использования нужна заказчику в кабинете:
    # по ней видно, какой из ключей ещё работает, а какой можно отозвать.
    api_token.last_used_at = utcnow()
    return account


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

    if organization is None:
        if alias is not None:
            raise OrganizationNotFoundError(
                f"Организация «{alias}» не найдена. Проверьте параметр org."
            )
        raise OrganizationNotFoundError(
            "У аккаунта нет организации по умолчанию. Укажите параметр org "
            "или отметьте одну из организаций основной."
        )
    return organization
