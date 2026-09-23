"""Выдача доступа в кабинет: одноразовые ссылки и письма к ним.

Модуль связывает три части — ссылки (:mod:`nosbp.cabinet.invites`), тексты
писем (:mod:`nosbp.mail.letters`) и настройки — и ничего не отправляет сам.
Каждая функция готовит письмо и возвращает его вызывающему коду: тот решает,
отправить его сразу или фоновой задачей после ответа.

Разделение не косметическое. Письмо восстановления обязано уходить фоновой
задачей: если отправлять его в обработчике запроса, время ответа выдало бы,
зарегистрирован адрес или нет.
"""

import datetime
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.cabinet import invites
from nosbp.cabinet.service import find_by_email
from nosbp.core.config import Settings
from nosbp.db.base import utcnow
from nosbp.db.models import Account, InvitePurpose
from nosbp.mail import letters
from nosbp.mail.base import Letter


@dataclass(frozen=True)
class Delivery:
    """Готовое письмо и адрес получателя."""

    to: str
    letter: Letter
    url: str = ""
    """Ссылка из письма. Нужна панели управления: оператор передаёт её
    заказчику вручную, если письмо не дошло."""


async def prepare_password_reset(
    db: AsyncSession, settings: Settings, *, email: str
) -> Delivery | None:
    """Готовит письмо для самостоятельного восстановления пароля.

    Возвращает ``None``, когда письмо отправлять не нужно: такого адреса
    нет, учётная запись отключена или на этот адрес уже отправлено
    столько писем, сколько разрешено за час. Во всех случаях страница
    отвечает одинаково — иначе по ответу можно было бы собрать список
    зарегистрированных адресов.

    Ссылки, выданные оператором через панель, тоже попадают в этот счёт:
    ограничение защищает почтовый ящик заказчика, а не сервис.
    """
    account = await find_by_email(db, email)
    if account is None or not account.is_active:
        return None

    if await _reset_limit_reached(db, settings, account):
        return None

    ttl_hours = settings.password_reset_ttl_hours
    token = await invites.issue(
        db, account, purpose=InvitePurpose.PASSWORD, ttl_hours=ttl_hours
    )
    url = _url(settings, token, InvitePurpose.PASSWORD)
    return Delivery(
        to=account.email,
        letter=letters.password_reset(url=url, ttl_hours=ttl_hours),
        url=url,
    )


async def _reset_limit_reached(
    db: AsyncSession, settings: Settings, account: Account
) -> bool:
    """Исчерпан ли часовой лимит писем о восстановлении."""
    issued = await invites.count_recent(
        db,
        account.id,
        purpose=InvitePurpose.PASSWORD,
        since=utcnow() - datetime.timedelta(hours=1),
    )
    return issued >= settings.password_reset_max_per_hour


async def prepare_access_invite(
    db: AsyncSession, settings: Settings, account: Account
) -> Delivery:
    """Готовит письмо с доступом для аккаунта, заведённого оператором.

    Срок жизни ссылки здесь длиннее, чем при самостоятельном
    восстановлении: заказчик не ждёт этого письма и может открыть его
    на следующий день.
    """
    ttl_hours = settings.cabinet_invite_ttl_hours
    token = await invites.issue(
        db, account, purpose=InvitePurpose.PASSWORD, ttl_hours=ttl_hours
    )
    url = _url(settings, token, InvitePurpose.PASSWORD)
    return Delivery(
        to=account.email,
        letter=letters.access_invite(url=url, ttl_hours=ttl_hours),
        url=url,
    )


async def prepare_email_confirmation(
    db: AsyncSession, settings: Settings, account: Account
) -> Delivery:
    """Готовит письмо с подтверждением адреса почты."""
    ttl_hours = settings.email_confirm_ttl_hours
    token = await invites.issue(
        db, account, purpose=InvitePurpose.EMAIL, ttl_hours=ttl_hours
    )
    url = _url(settings, token, InvitePurpose.EMAIL)
    return Delivery(
        to=account.email,
        letter=letters.email_confirmation(url=url, ttl_hours=ttl_hours),
        url=url,
    )


def password_changed_notice(
    settings: Settings, *, email: str, address: str
) -> Delivery:
    """Готовит уведомление о смене пароля.

    Ничего не пишет в базу: письмо только сообщает владельцу адреса
    о том, что уже произошло.
    """
    return Delivery(
        to=email,
        letter=letters.password_changed(
            moment=utcnow(),
            address=address,
            support_email=settings.support_email,
        ),
    )


def _url(settings: Settings, token: str, purpose: InvitePurpose) -> str:
    """Собирает адрес страницы, на которую ведёт ссылка из письма."""
    return invites.build_url(
        settings.public_base_url,
        settings.cabinet_prefix,
        token,
        purpose=purpose,
    )
