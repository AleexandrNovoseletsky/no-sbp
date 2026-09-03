"""Консольные команды управления администраторами.

Учётные записи администраторов создаются только из консоли: регистрация
через веб-интерфейс не предусмотрена, поскольку панель управляет
балансами заказчиков.
"""

import argparse
import getpass
import sys
from typing import Final

import segno
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin.security import (
    generate_totp_secret,
    hash_password,
    totp_provisioning_uri,
    validate_password_strength,
)
from nosbp.admin.service import AdminAuthService
from nosbp.core.config import get_settings
from nosbp.db.base import utcnow
from nosbp.db.models import AdminUser
from nosbp.db.session import get_session_factory

PASSWORD_PROMPT: Final[str] = "Пароль: "
PASSWORD_REPEAT_PROMPT: Final[str] = "Ещё раз: "
DATETIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"


def _ask_password() -> str:
    """Спрашивает пароль дважды, не показывая ввод.

    :raises SystemExit: если пароли не совпали, пароль слабый или команда
        запущена не из терминала.
    """
    require_tty()
    password = getpass.getpass(PASSWORD_PROMPT)
    if password != getpass.getpass(PASSWORD_REPEAT_PROMPT):
        raise SystemExit("Пароли не совпали.")

    complaint = validate_password_strength(password)
    if complaint is not None:
        raise SystemExit(complaint)
    return password


def _print_totp(secret: str, email: str) -> None:
    """Печатает секрет и QR для приложения-аутентификатора.

    QR-код выводится в терминал средствами segno, что избавляет от
    необходимости передавать изображение отдельным каналом.
    """
    uri = totp_provisioning_uri(secret, email)

    print("\nВторой фактор. Отсканируйте код приложением-аутентификатором")
    print("(Google Authenticator, Aegis, 1Password — любым):\n")
    segno.make_qr(uri).terminal(compact=True)
    print(f"\nЕсли отсканировать не получается, введите секрет вручную:\n  {secret}\n")
    print("Сохраните его в надёжном месте: заново он не показывается.")


async def _find_admin(session: AsyncSession, email: str) -> AdminUser:
    """Находит администратора по адресу почты."""
    result = await session.execute(
        select(AdminUser).where(AdminUser.email == email.strip().lower())
    )
    admin = result.scalar_one_or_none()
    if admin is None:
        raise SystemExit(f"Администратор {email} не найден.")
    return admin


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def cmd_admin_create(args: argparse.Namespace) -> None:
    """Заводит администратора панели."""
    email = args.email.strip().lower()
    password = _ask_password()
    secret = generate_totp_secret()

    async with get_session_factory()() as session:
        existing = await session.execute(
            select(AdminUser).where(AdminUser.email == email)
        )
        if existing.scalar_one_or_none() is not None:
            raise SystemExit(f"Администратор {email} уже заведён.")

        session.add(
            AdminUser(
                email=email,
                password_hash=hash_password(password),
                totp_secret=secret,
            )
        )
        await session.commit()

    print(f"\nАдминистратор создан: {email}")
    _print_totp(secret, email)

    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    print(f"\nВход: {base_url}{settings.admin_prefix}/login")


async def cmd_admin_list(args: argparse.Namespace) -> None:
    """Показывает администраторов."""
    async with get_session_factory()() as session:
        result = await session.execute(select(AdminUser).order_by(AdminUser.created_at))
        admins = list(result.scalars())

    if not admins:
        print("Администраторов нет. Заведите первого: nosbp admin create --email …")
        return

    print(f"{'Почта':<36} {'Состояние':<12} {'2ФА':<6} Последний вход")
    for admin in admins:
        state = "работает" if admin.is_active else "отключён"
        totp = "есть" if admin.totp_secret else "нет"
        last = (
            admin.last_login_at.strftime(DATETIME_FORMAT)
            if admin.last_login_at
            else "ни разу"
        )
        print(f"{admin.email:<36} {state:<12} {totp:<6} {last}")


async def cmd_admin_password(args: argparse.Namespace) -> None:
    """Меняет пароль администратора и закрывает все его сессии."""
    password = _ask_password()

    async with get_session_factory()() as session:
        admin = await _find_admin(session, args.email)
        admin.password_hash = hash_password(password)
        admin.failed_attempts = 0
        admin.locked_until = None
        await session.commit()

        # Смена пароля должна выкидывать все открытые сессии: иначе тот,
        # кто увёл старый пароль, останется внутри.
        closed = await AdminAuthService(session, get_settings()).close_all_sessions(
            admin.id
        )

    print(f"Пароль изменён. Закрыто сессий: {closed}.")


async def cmd_admin_reset_totp(args: argparse.Namespace) -> None:
    """Перевыпускает секрет второго фактора — например, при потере телефона."""
    secret = generate_totp_secret()

    async with get_session_factory()() as session:
        admin = await _find_admin(session, args.email)
        admin.totp_secret = secret
        admin.failed_attempts = 0
        admin.locked_until = None
        await session.commit()
        closed = await AdminAuthService(session, get_settings()).close_all_sessions(
            admin.id
        )

    print(f"Второй фактор перевыпущен. Закрыто сессий: {closed}.")
    _print_totp(secret, args.email)


async def cmd_admin_disable(args: argparse.Namespace) -> None:
    """Отключает или включает администратора."""
    async with get_session_factory()() as session:
        admin = await _find_admin(session, args.email)
        admin.is_active = args.enable
        await session.commit()
        closed = 0
        if not args.enable:
            closed = await AdminAuthService(session, get_settings()).close_all_sessions(
                admin.id
            )

    state = "включён" if args.enable else "отключён"
    print(f"Администратор {admin.email} {state}. Закрыто сессий: {closed}.")


async def cmd_admin_unlock(args: argparse.Namespace) -> None:
    """Снимает блокировку после неудачных попыток входа."""
    async with get_session_factory()() as session:
        admin = await _find_admin(session, args.email)
        if admin.locked_until is None or admin.locked_until <= utcnow():
            print("Вход и так не заблокирован.")
            return
        admin.locked_until = None
        admin.failed_attempts = 0
        await session.commit()

    print(f"Блокировка входа для {admin.email} снята.")


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------


def register_admin_commands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Добавляет группу команд ``admin`` в общий разбор командной строки."""
    admin = subparsers.add_parser(
        "admin", help="Администраторы панели управления"
    ).add_subparsers(dest="subcommand", required=True)

    create = admin.add_parser("create", help="Завести администратора")
    create.add_argument("--email", required=True)
    create.set_defaults(func=cmd_admin_create)

    listing = admin.add_parser("list", help="Показать администраторов")
    listing.set_defaults(func=cmd_admin_list)

    password = admin.add_parser("password", help="Сменить пароль")
    password.add_argument("--email", required=True)
    password.set_defaults(func=cmd_admin_password)

    totp = admin.add_parser("reset-totp", help="Перевыпустить второй фактор")
    totp.add_argument("--email", required=True)
    totp.set_defaults(func=cmd_admin_reset_totp)

    disable = admin.add_parser("disable", help="Отключить администратора")
    disable.add_argument("--email", required=True)
    disable.set_defaults(func=cmd_admin_disable, enable=False)

    enable = admin.add_parser("enable", help="Включить администратора")
    enable.add_argument("--email", required=True)
    enable.set_defaults(func=cmd_admin_disable, enable=True)

    unlock = admin.add_parser("unlock", help="Снять блокировку входа")
    unlock.add_argument("--email", required=True)
    unlock.set_defaults(func=cmd_admin_unlock)


def require_tty() -> None:
    """Проверяет, что команда запущена в терминале.

    Пароль запрашивается интерактивно; чтение из стандартного ввода
    привело бы к его сохранению в истории команд.
    """
    if not sys.stdin.isatty():
        raise SystemExit(
            "Команда требует терминала: запустите её с флагом -it, "
            "например «docker compose exec api nosbp admin create --email …»."
        )
