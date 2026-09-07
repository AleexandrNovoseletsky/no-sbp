"""Одноразовые ссылки, которые уходят заказчику на почту.

Один механизм закрывает три случая:

* первый вход в аккаунт, заведённый оператором через панель управления;
* восстановление забытого пароля по запросу самого заказчика;
* подтверждение адреса почты при регистрации.

Различает их назначение ссылки. В базе лежит только хэш токена, ссылка
одноразовая, а прежние ссылки того же назначения гасятся при выдаче новой.
"""

import datetime
import secrets
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.errors import ValidationError
from nosbp.core.security import hash_password as make_password_hash
from nosbp.core.security import hash_session_token, validate_password_strength
from nosbp.db.base import utcnow
from nosbp.db.models import Account, AccountInvite, InvitePurpose

TOKEN_BYTES: Final = 32

PATHS: Final[Mapping[InvitePurpose, str]] = MappingProxyType(
    {
        InvitePurpose.PASSWORD: "/password",
        InvitePurpose.EMAIL: "/confirm",
    }
)
"""Путь страницы внутри кабинета для каждого назначения ссылки."""

BAD_LINK_MESSAGE: Final[str] = (
    "Ссылка недействительна или уже использована. Запросите новую."
)
"""Единый текст на все неудачи.

Подсказывать, что именно не так — истёк срок, ссылка уже использована или
такого токена не было вовсе, — значит помогать перебору.
"""


async def issue(
    db: AsyncSession,
    account: Account,
    *,
    purpose: InvitePurpose,
    ttl_hours: int,
) -> str:
    """Выпускает ссылку и возвращает её токен открытым текстом.

    Прежние неиспользованные ссылки того же назначения закрываются:
    у одного аккаунта не должно быть нескольких действующих ссылок сразу.
    Ссылки другого назначения не трогаются — запрос пароля не должен
    отменять начатое подтверждение адреса.

    :return: токен для подстановки в адрес. Больше нигде не хранится.
    """
    now = utcnow()
    await _revoke_pending(db, account.id, purpose, now)

    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        AccountInvite(
            account_id=account.id,
            token_hash=hash_session_token(token),
            purpose=purpose,
            expires_at=now + datetime.timedelta(hours=ttl_hours),
        )
    )
    await db.flush()
    return token


async def _revoke_pending(
    db: AsyncSession,
    account_id: uuid.UUID,
    purpose: InvitePurpose,
    now: datetime.datetime,
) -> None:
    """Помечает использованными прежние действующие ссылки."""
    result = await db.execute(
        select(AccountInvite).where(
            AccountInvite.account_id == account_id,
            AccountInvite.purpose == purpose,
            AccountInvite.used_at.is_(None),
        )
    )
    for invite in result.scalars():
        invite.used_at = now


async def count_recent(
    db: AsyncSession,
    account_id: uuid.UUID,
    *,
    purpose: InvitePurpose,
    since: datetime.datetime,
) -> int:
    """Считает, сколько ссылок выдано аккаунту с указанного момента.

    На этом держится ограничение частоты: чужой почтовый ящик нельзя
    завалить письмами, повторяя форму восстановления.
    """
    result = await db.execute(
        select(func.count())
        .select_from(AccountInvite)
        .where(
            AccountInvite.account_id == account_id,
            AccountInvite.purpose == purpose,
            AccountInvite.created_at >= since,
        )
    )
    return int(result.scalar_one())


async def find_account(
    db: AsyncSession, token: str, *, purpose: InvitePurpose
) -> Account:
    """Находит заказчика по действующей ссылке нужного назначения.

    :raises ValidationError: ссылка неизвестна, использована, истекла или
        выдана для другого действия.
    """
    invite = await _find_invite(db, token, purpose)
    if invite is None:
        raise ValidationError(BAD_LINK_MESSAGE)

    return await _account_of(db, invite)


async def _account_of(db: AsyncSession, invite: AccountInvite) -> Account:
    """Загружает заказчика, которому выдана ссылка.

    :raises ValidationError: аккаунт удалён или отключён оператором.
    """
    account = await db.get(Account, invite.account_id)
    if account is None or not account.is_active:
        raise ValidationError("Учётная запись недоступна. Напишите в поддержку.")
    return account


async def _find_invite(
    db: AsyncSession, token: str, purpose: InvitePurpose
) -> AccountInvite | None:
    """Находит действующую ссылку по токену."""
    if not token:
        return None

    result = await db.execute(
        select(AccountInvite).where(
            AccountInvite.token_hash == hash_session_token(token),
            AccountInvite.purpose == purpose,
        )
    )
    invite = result.scalar_one_or_none()
    if invite is None or not invite.is_usable(utcnow()):
        return None
    return invite


async def use(db: AsyncSession, token: str, password: str) -> Account:
    """Устанавливает пароль по ссылке и гасит её.

    Заодно отмечает адрес подтверждённым: ссылка уходила на почту, и,
    открыв её, человек доказал доступ к ящику.

    :raises ValidationError: ссылка недействительна или пароль слаб.
    """
    complaint = validate_password_strength(password)
    if complaint is not None:
        raise ValidationError(complaint)

    invite = await _find_invite(db, token, InvitePurpose.PASSWORD)
    if invite is None:
        raise ValidationError(BAD_LINK_MESSAGE)

    account = await _account_of(db, invite)
    now = utcnow()

    account.password_hash = make_password_hash(password)
    account.failed_attempts = 0
    account.locked_until = None
    _mark_confirmed(account, now)
    invite.used_at = now
    return account


async def confirm(db: AsyncSession, token: str) -> Account:
    """Подтверждает адрес почты по ссылке и гасит её.

    :raises ValidationError: ссылка недействительна.
    """
    invite = await _find_invite(db, token, InvitePurpose.EMAIL)
    if invite is None:
        raise ValidationError(BAD_LINK_MESSAGE)

    account = await _account_of(db, invite)
    now = utcnow()

    _mark_confirmed(account, now)
    invite.used_at = now
    return account


def _mark_confirmed(account: Account, now: datetime.datetime) -> None:
    """Отмечает адрес подтверждённым, если это ещё не сделано.

    Повторное подтверждение не сдвигает дату: она показывает, когда
    владелец впервые доказал доступ к ящику.
    """
    if account.email_confirmed_at is None:
        account.email_confirmed_at = now


def build_url(
    base_url: str, cabinet_prefix: str, token: str, *, purpose: InvitePurpose
) -> str:
    """Собирает полный адрес страницы, на которую ведёт ссылка."""
    return f"{base_url.rstrip('/')}{cabinet_prefix}{PATHS[purpose]}/{token}"
