"""Одноразовые ссылки для установки пароля заказчика.

Один механизм закрывает два случая: вход в аккаунт, заведённый оператором
через панель управления, и восстановление забытого пароля. Ссылку выдаёт
оператор и передаёт заказчику любым доступным каналом.
"""

import datetime
import secrets
import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.errors import ValidationError
from nosbp.core.security import hash_password as make_password_hash
from nosbp.core.security import hash_session_token, validate_password_strength
from nosbp.db.base import utcnow
from nosbp.db.models import Account, AccountInvite

TOKEN_BYTES: Final = 32
INVITE_PATH: Final[str] = "/password"
"""Путь страницы установки пароля внутри кабинета."""


async def issue(db: AsyncSession, account: Account, *, ttl_hours: int) -> str:
    """Выпускает ссылку и возвращает её токен открытым текстом.

    Прежние неиспользованные ссылки этого заказчика закрываются: у одного
    аккаунта не должно быть нескольких действующих ссылок сразу.

    :return: токен, который подставляется в адрес. Больше нигде не хранится.
    """
    now = utcnow()
    await _revoke_pending(db, account.id, now)

    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(
        AccountInvite(
            account_id=account.id,
            token_hash=hash_session_token(token),
            expires_at=now + datetime.timedelta(hours=ttl_hours),
        )
    )
    await db.flush()
    return token


async def _revoke_pending(
    db: AsyncSession, account_id: uuid.UUID, now: datetime.datetime
) -> None:
    """Помечает использованными прежние действующие ссылки."""
    result = await db.execute(
        select(AccountInvite).where(
            AccountInvite.account_id == account_id,
            AccountInvite.used_at.is_(None),
        )
    )
    for invite in result.scalars():
        invite.used_at = now


async def find_account(db: AsyncSession, token: str) -> Account:
    """Находит заказчика по действующей ссылке.

    :raises ValidationError: ссылка неизвестна, использована или истекла.
        Текст одинаков во всех случаях: подсказывать, что именно не так,
        значит помогать перебору.
    """
    invite = await _find_invite(db, token)
    if invite is None:
        raise ValidationError(
            "Ссылка недействительна или уже использована. Запросите новую в поддержке."
        )

    account = await db.get(Account, invite.account_id)
    if account is None or not account.is_active:
        raise ValidationError("Учётная запись недоступна. Напишите в поддержку.")
    return account


async def _find_invite(db: AsyncSession, token: str) -> AccountInvite | None:
    """Находит действующую ссылку по токену."""
    if not token:
        return None

    result = await db.execute(
        select(AccountInvite).where(
            AccountInvite.token_hash == hash_session_token(token)
        )
    )
    invite = result.scalar_one_or_none()
    if invite is None or not invite.is_usable(utcnow()):
        return None
    return invite


async def use(db: AsyncSession, token: str, password: str) -> Account:
    """Устанавливает пароль по ссылке и гасит её.

    :raises ValidationError: ссылка недействительна или пароль слаб.
    """
    complaint = validate_password_strength(password)
    if complaint is not None:
        raise ValidationError(complaint)

    invite = await _find_invite(db, token)
    if invite is None:
        raise ValidationError(
            "Ссылка недействительна или уже использована. Запросите новую в поддержке."
        )

    account = await find_account(db, token)
    account.password_hash = make_password_hash(password)
    account.failed_attempts = 0
    account.locked_until = None
    invite.used_at = utcnow()
    return account


def build_url(base_url: str, cabinet_prefix: str, token: str) -> str:
    """Собирает полный адрес страницы установки пароля."""
    return f"{base_url.rstrip('/')}{cabinet_prefix}{INVITE_PATH}/{token}"
