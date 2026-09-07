"""Консольная проверка почты.

Настройки почты — единственная часть сервиса, которую нельзя проверить,
не выходя наружу: сервер, порт, режим шифрования и пароль приложения
проверяются только настоящим соединением. Команда ``nosbp mail check``
делает это отдельно от остального, чтобы разбираться с почтой до того,
как её начнут ждать заказчики.
"""

import argparse
import asyncio
from typing import Final

import aiosmtplib

from nosbp.core.config import Settings, get_settings
from nosbp.mail import letters
from nosbp.mail.service import build_mailer

CHECK_SUBJECT_HINT: Final[str] = (
    "Письмо уйдёт настоящее — укажите свой адрес, а не адрес заказчика."
)

TEST_LINK_PATH: Final[str] = "/cabinet/password/proverka"
"""Ссылка в пробном письме никуда не ведёт: проверяется доставка,
а не работа кабинета."""


def _describe(settings: Settings) -> None:
    """Печатает, с какими настройками выполняется проверка."""
    print("Настройки почты:")
    print(f"  сервер:      {settings.smtp_host}:{settings.smtp_effective_port}")
    print(f"  шифрование:  {settings.smtp_security}")
    print(f"  логин:       {settings.smtp_user or '(без авторизации)'}")
    print(f"  от кого:     {settings.mail_from_name} <{settings.mail_from}>")


async def _connect(settings: Settings) -> None:
    """Соединяется с сервером и авторизуется, ничего не отправляя.

    :raises SystemExit: соединение или авторизация не удались.
    """
    client = aiosmtplib.SMTP(
        hostname=settings.smtp_host,
        port=settings.smtp_effective_port,
        use_tls=settings.smtp_security == "ssl",
        start_tls=settings.smtp_security == "starttls",
        timeout=settings.smtp_timeout_seconds,
    )
    try:
        await client.connect()
        if settings.smtp_user:
            await client.login(settings.smtp_user, settings.smtp_password)
        await client.quit()
    except Exception as error:
        raise SystemExit(f"Соединение не удалось: {error}") from error


async def cmd_mail_check(args: argparse.Namespace) -> None:
    """Проверяет настройки почты, при желании отправляя пробное письмо."""
    settings = get_settings()
    if not settings.mail_configured:
        raise SystemExit(
            "Почта не настроена: задайте SMTP_HOST и SMTP_USER в .env. "
            "Пока настройки пусты, письма не отправляются, а их текст "
            "пишется в журнал."
        )

    _describe(settings)

    print("\nПроверяю соединение и пароль…")
    await _connect(settings)
    print("Соединение установлено, пароль принят.")

    if not args.to:
        print(
            "\nПисьмо не отправлялось. Чтобы проверить доставку, повторите "
            "команду с «--to свой@адрес»."
        )
        return

    print(f"\nОтправляю пробное письмо на {args.to}…")
    letter = letters.password_reset(
        url=f"{settings.public_base_url.rstrip('/')}{TEST_LINK_PATH}",
        ttl_hours=settings.password_reset_ttl_hours,
    )
    if await build_mailer(settings).send(args.to, letter):
        print("Письмо отправлено. Проверьте ящик, в том числе папку «Спам».")
        print(
            "Если письмо в спаме — не настроены записи SPF, DKIM и DMARC "
            "для домена отправителя."
        )
    else:
        raise SystemExit("Отправить письмо не удалось, подробности в журнале.")


def register_mail_commands(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Добавляет группу команд ``mail`` в общий разбор командной строки."""
    mail = subparsers.add_parser("mail", help="Почта").add_subparsers(
        dest="subcommand", required=True
    )

    check = mail.add_parser("check", help="Проверить настройки почты")
    check.add_argument("--to", default="", help=CHECK_SUBJECT_HINT)
    check.set_defaults(func=cmd_mail_check)


def main() -> None:
    """Точка входа для запуска модуля напрямую."""
    parser = argparse.ArgumentParser(prog="nosbp mail")
    register_mail_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args()
    asyncio.run(args.func(args))
