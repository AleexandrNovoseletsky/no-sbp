"""Защита от подбора пароля.

Логика общая для панели управления и личного кабинета: после нескольких
неудачных попыток вход временно закрывается независимо от того, верны ли
следующие учётные данные.
"""

import datetime
from typing import Protocol

from nosbp.core.errors import NosbpError


class Lockable(Protocol):
    """Учётная запись, вход в которую можно временно заблокировать."""

    failed_attempts: int
    locked_until: datetime.datetime | None


def ensure_not_locked(
    account: Lockable,
    now: datetime.datetime,
    error: type[NosbpError],
) -> None:
    """Проверяет, что вход не заблокирован.

    :param error: класс исключения, которым сообщать о блокировке.
    :raises NosbpError: если блокировка ещё действует.
    """
    if account.locked_until is None or now >= account.locked_until:
        return

    seconds_left = (account.locked_until - now).total_seconds()
    minutes_left = max(1, int(seconds_left // 60) + 1)
    raise error(f"Слишком много неудачных попыток. Повторите через {minutes_left} мин.")


def register_failure(
    account: Lockable,
    now: datetime.datetime,
    *,
    max_attempts: int,
    lockout_minutes: int,
) -> None:
    """Учитывает неудачную попытку и при переполнении закрывает вход."""
    account.failed_attempts += 1
    if account.failed_attempts >= max_attempts:
        account.locked_until = now + datetime.timedelta(minutes=lockout_minutes)
        account.failed_attempts = 0


def reset_failures(account: Lockable) -> None:
    """Сбрасывает счётчик после успешного входа."""
    account.failed_attempts = 0
    account.locked_until = None
