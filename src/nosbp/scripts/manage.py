"""Консольная утилита администрирования.

Пока нет личного кабинета, аккаунты, организации и ключи заводятся отсюда.
Запуск в контейнере::

    docker compose exec api nosbp account create --email a@b.ru --name "ООО Ромашка"
    docker compose exec api nosbp org add --email a@b.ru --alias main ...
    docker compose exec api nosbp token issue --email a@b.ru --label RetailCRM
    docker compose exec api nosbp topup --email a@b.ru --rubles 1000
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin import crud
from nosbp.admin.cli import register_admin_commands
from nosbp.admin.logos import store_logo
from nosbp.admin.stats import format_fee_percent, parse_fee_percent
from nosbp.billing.service import BillingService, calculate_balance_from_ledger
from nosbp.core.config import Settings, get_settings
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.errors import NosbpError
from nosbp.core.money import format_roubles, to_kopecks
from nosbp.db.base import utcnow
from nosbp.db.models import Account, ApiToken, LedgerEntry
from nosbp.db.session import dispose_engine, get_session_factory
from nosbp.mail.cli import register_mail_commands
from nosbp.storage.s3 import S3Storage

DEFAULT_STATEMENT_LIMIT: Final = 20
DATETIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"
AMOUNT_COLUMN_WIDTH: Final = 14
"""Ширина колонки суммы в выписке — чтобы цифры выстроились по разряду."""

NO_DAILY_LIMIT: Final = 0
"""Значение --daily-limit, снимающее суточное ограничение."""


async def _find_account(session: AsyncSession, email: str) -> Account:
    """Находит аккаунт по адресу почты."""
    result = await session.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if account is None:
        raise SystemExit(f"Аккаунт {email} не найден.")
    return account


def _resolve_daily_limit(requested: int | None, settings: Settings) -> int | None:
    """Переводит аргумент --daily-limit в значение для колонки.

    Не передан — берётся значение по умолчанию из настроек; ноль означает
    «без ограничения»; всё остальное считается рублями.
    """
    if requested is None:
        return settings.default_daily_charge_limit_kopecks
    if requested == NO_DAILY_LIMIT:
        return None
    return to_kopecks(requested)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def cmd_account_create(args: argparse.Namespace) -> None:
    """Создаёт учётную запись заказчика."""
    settings = get_settings()
    limit = _resolve_daily_limit(args.daily_limit, settings)

    async with get_session_factory()() as session:
        account = await crud.create_account(
            session,
            settings,
            email=args.email,
            display_name=args.name,
            daily_limit_kopecks=limit,
            is_unlimited=args.unlimited,
            use_default_limit=False,
        )
        await session.commit()

    shown = (
        "без ограничения" if limit is None else format_roubles(limit, fractional=False)
    )
    print(f"Аккаунт создан: {account.email}")
    print(f"  id:                {account.id}")
    print(f"  суточный лимит:    {shown}")
    print(f"  безлимит:          {'да' if account.is_unlimited else 'нет'}")


async def cmd_org_add(args: argparse.Namespace) -> None:
    """Добавляет организацию-получателя платежа с проверкой реквизитов."""
    settings = get_settings()
    data = crud.RequisitesInput(
        alias=args.alias,
        name=args.name,
        personal_acc=args.account,
        bank_name=args.bank,
        bic=args.bic,
        corresp_acc=args.corr,
        payee_inn=args.inn,
        kpp=args.kpp,
        qr_color=args.color,
        acquiring_fee_bps=(
            parse_fee_percent(args.fee)
            if args.fee
            else settings.default_acquiring_fee_bps
        ),
        average_check_kopecks=(
            to_kopecks(args.average_check) if args.average_check else None
        ),
        is_default=args.default,
    )

    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        organization = await crud.create_organization(session, account, data)

        if args.logo:
            organization.logo_key = await _upload_logo(Path(args.logo), account.id)
        logo_key = organization.logo_key

        await session.commit()

    print(f"Организация добавлена: {organization.alias} — {organization.name}")
    print(f"  цвет QR:           {organization.qr_color}")
    print(f"  логотип:           {logo_key or 'нет'}")
    print(f"  ставка эквайринга: {format_fee_percent(organization.acquiring_fee_bps)}")
    print(f"  по умолчанию:      {'да' if organization.is_default else 'нет'}")


async def _upload_logo(path: Path, account_id: uuid.UUID) -> str:
    """Кладёт логотип в объектное хранилище и возвращает его ключ.

    Проверки — те же, что в панели: формат, размер и то, что файл
    действительно читается как картинка.
    """
    if not path.exists():  # noqa: ASYNC240
        raise SystemExit(f"Файл логотипа не найден: {path}")

    settings = get_settings()
    storage = S3Storage(settings)
    storage.ensure_bucket()
    return await store_logo(
        storage=storage,
        account_id=account_id,
        filename=path.name,
        data=path.read_bytes(),  # noqa: ASYNC240
        max_bytes=settings.logo_max_bytes,
    )


async def cmd_token_issue(args: argparse.Namespace) -> None:
    """Выпускает новый ключ API."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        token = await crud.issue_token(session, account, label=args.label)
        await session.commit()

    base_url = get_settings().public_base_url.rstrip("/")
    print("Ключ выпущен. Он показывается ОДИН раз — сохраните его сейчас:\n")
    print(f"  {token}\n")
    print("Пример вставки в шаблон письма:")
    print(
        f'  <img src="{base_url}/v1/qr?token={token}'
        '&sum=4700000&purpose=Заказ+1234" alt="QR для оплаты">'
    )


async def cmd_token_revoke(args: argparse.Namespace) -> None:
    """Отзывает ключ по его префиксу."""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(ApiToken).where(ApiToken.prefix == args.prefix)
        )
        tokens = list(result.scalars())
        if not tokens:
            raise SystemExit(f"Ключ с префиксом {args.prefix} не найден.")

        revoked_at = utcnow()
        for api_token in tokens:
            api_token.revoked_at = revoked_at
        await session.commit()

    print(f"Отозвано ключей: {len(tokens)}")


async def cmd_topup(args: argparse.Namespace) -> None:
    """Пополняет баланс заказчика."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        billing = BillingService(session, get_settings())
        locked = await billing.lock_account(account.id)
        await billing.topup(
            locked, to_kopecks(args.rubles), comment=args.comment or "Пополнение"
        )
        await session.commit()
        print(f"Баланс {account.email}: {format_roubles(locked.balance_kopecks)}")


async def cmd_refund(args: argparse.Namespace) -> None:
    """Возвращает деньги на баланс заказчика."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        billing = BillingService(session, get_settings())
        locked = await billing.lock_account(account.id)
        await billing.refund(locked, to_kopecks(args.rubles), comment=args.comment)
        await session.commit()
        print(f"Баланс {account.email}: {format_roubles(locked.balance_kopecks)}")


async def cmd_balance(args: argparse.Namespace) -> None:
    """Показывает баланс и последние операции."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        from_ledger = await calculate_balance_from_ledger(session, account.id)

        print(f"Аккаунт:  {account.email}")
        print(f"Баланс:   {format_roubles(account.balance_kopecks)}")
        if from_ledger != account.balance_kopecks:
            print(
                f"ВНИМАНИЕ: журнал даёт {format_roubles(from_ledger)} — "
                "кэш баланса разошёлся с журналом."
            )
        if account.overdraft_until is not None:
            print(f"Овердрафт до: {account.overdraft_until:{DATETIME_FORMAT}} UTC")

        entries = await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(args.limit)
        )
        print(f"\nПоследние операции (до {args.limit}):")
        for entry in entries.scalars():
            amount = format_roubles(entry.amount_kopecks)
            print(
                f"  {entry.created_at:{DATETIME_FORMAT}}  "
                f"{amount:>{AMOUNT_COLUMN_WIDTH}}  "
                f"{entry.entry_type:<12} {entry.comment}"
            )


# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Собирает разбор командной строки."""
    parser = argparse.ArgumentParser(
        prog="nosbp", description="Администрирование сервиса NoSBP."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    account = sub.add_parser("account", help="Учётные записи").add_subparsers(
        dest="subcommand", required=True
    )
    create = account.add_parser("create", help="Создать аккаунт")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True, help="Название компании")
    create.add_argument(
        "--daily-limit",
        type=int,
        default=None,
        help=(
            "Суточный потолок списаний в рублях. "
            f"{NO_DAILY_LIMIT} — снять ограничение. "
            "Не указан — взять значение из настроек."
        ),
    )
    create.add_argument(
        "--unlimited",
        action="store_true",
        help="Безлимит: счета генерируются бесплатно.",
    )
    create.set_defaults(func=cmd_account_create)

    org = sub.add_parser("org", help="Организации-получатели").add_subparsers(
        dest="subcommand", required=True
    )
    add = org.add_parser("add", help="Добавить организацию")
    add.add_argument("--email", required=True, help="Почта аккаунта")
    add.add_argument("--alias", required=True, help="Короткое имя для ?org=")
    add.add_argument("--name", required=True, help="Наименование получателя")
    add.add_argument("--account", required=True, help="Расчётный счёт, 20 цифр")
    add.add_argument("--bank", required=True, help="Наименование банка")
    add.add_argument("--bic", required=True, help="БИК, 9 цифр")
    add.add_argument("--corr", required=True, help="Корр. счёт, 20 цифр")
    add.add_argument("--inn", required=True, help="ИНН, 10 или 12 цифр")
    add.add_argument("--kpp", default=None, help="КПП, 9 цифр (у ИП нет)")
    add.add_argument("--color", default=DEFAULT_QR_COLOR, help="Цвет QR, #RRGGBB")
    add.add_argument("--logo", default=None, help="Путь к PNG или JPEG логотипа")
    add.add_argument(
        "--fee", default=None, help="Ставка эквайринга в процентах, например 0,7"
    )
    add.add_argument(
        "--average-check",
        type=int,
        default=None,
        help="Средний чек в рублях — для счетов без указанной суммы",
    )
    add.add_argument("--default", action="store_true", help="Сделать основной")
    add.set_defaults(func=cmd_org_add)

    token = sub.add_parser("token", help="Ключи API").add_subparsers(
        dest="subcommand", required=True
    )
    issue = token.add_parser("issue", help="Выпустить ключ")
    issue.add_argument("--email", required=True)
    issue.add_argument("--label", default="", help="Имя ключа: RetailCRM, Сайт…")
    issue.set_defaults(func=cmd_token_issue)

    revoke = token.add_parser("revoke", help="Отозвать ключ")
    revoke.add_argument("--prefix", required=True, help="Открытая часть ключа")
    revoke.set_defaults(func=cmd_token_revoke)

    topup = sub.add_parser("topup", help="Пополнить баланс")
    topup.add_argument("--email", required=True)
    topup.add_argument("--rubles", type=int, required=True)
    topup.add_argument("--comment", default=None)
    topup.set_defaults(func=cmd_topup)

    refund = sub.add_parser("refund", help="Вернуть деньги на баланс")
    refund.add_argument("--email", required=True)
    refund.add_argument("--rubles", type=int, required=True)
    refund.add_argument("--comment", required=True, help="Причина возврата")
    refund.set_defaults(func=cmd_refund)

    balance = sub.add_parser("balance", help="Показать баланс и выписку")
    balance.add_argument("--email", required=True)
    balance.add_argument("--limit", type=int, default=DEFAULT_STATEMENT_LIMIT)
    balance.set_defaults(func=cmd_balance)

    register_admin_commands(sub)
    register_mail_commands(sub)

    return parser


async def _run(args: argparse.Namespace) -> None:
    """Выполняет команду и закрывает соединения."""
    try:
        await args.func(args)
    finally:
        await dispose_engine()


def main() -> None:
    """Точка входа консольной утилиты."""
    args = build_parser().parse_args()
    try:
        asyncio.run(_run(args))
    except NosbpError as error:
        print(f"Ошибка: {error.message}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
