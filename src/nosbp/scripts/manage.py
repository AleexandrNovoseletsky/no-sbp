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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.billing.service import BillingService, calculate_balance_from_ledger
from nosbp.core.config import get_settings
from nosbp.core.errors import NosbpError
from nosbp.db.models import Account, ApiToken, LedgerEntry, Organization
from nosbp.db.session import dispose_engine, get_session_factory
from nosbp.payments.qr import validate_qr_color
from nosbp.payments.requisites import (
    validate_account,
    validate_bic,
    validate_inn,
    validate_kpp,
)
from nosbp.payments.tokens import generate_token, hash_token, token_prefix
from nosbp.storage.s3 import S3Storage

LOGO_CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


async def _find_account(session: AsyncSession, email: str) -> Account:
    """Находит аккаунт по адресу почты."""
    result = await session.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if account is None:
        raise SystemExit(f"Аккаунт {email} не найден.")
    return account


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


async def cmd_account_create(args: argparse.Namespace) -> None:
    """Создаёт учётную запись заказчика."""
    settings = get_settings()
    limit = (
        settings.default_daily_charge_limit_kopecks
        if args.daily_limit is None
        else (None if args.daily_limit == 0 else args.daily_limit * 100)
    )
    async with get_session_factory()() as session:
        account = Account(
            email=args.email,
            display_name=args.name,
            daily_charge_limit_kopecks=limit,
        )
        session.add(account)
        await session.commit()

    shown = "без ограничения" if limit is None else f"{limit / 100:.0f} ₽"
    print(f"Аккаунт создан: {account.email}")
    print(f"  id:                {account.id}")
    print(f"  суточный лимит:    {shown}")


async def cmd_org_add(args: argparse.Namespace) -> None:
    """Добавляет организацию-получателя платежа с проверкой реквизитов."""
    bic = validate_bic(args.bic)
    personal_acc = validate_account(args.account, bic)
    corresp_acc = validate_account(args.corr, bic)
    inn = validate_inn(args.inn)
    kpp = validate_kpp(args.kpp)
    color = validate_qr_color(args.color)

    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)

        logo_key: str | None = None
        if args.logo:
            logo_key = await _upload_logo(Path(args.logo), account.id)

        organization = Organization(
            account_id=account.id,
            alias=args.alias,
            name=args.name,
            personal_acc=personal_acc,
            bank_name=args.bank,
            bic=bic,
            corresp_acc=corresp_acc,
            payee_inn=inn,
            kpp=kpp,
            qr_color=color,
            logo_key=logo_key,
            is_default=args.default,
        )
        if args.default:
            # Организация по умолчанию может быть только одна.
            existing = await session.execute(
                select(Organization).where(
                    Organization.account_id == account.id,
                    Organization.is_default.is_(True),
                )
            )
            for other in existing.scalars():
                other.is_default = False

        session.add(organization)
        await session.commit()

    print(f"Организация добавлена: {organization.alias} — {organization.name}")
    print(f"  цвет QR:           {organization.qr_color}")
    print(f"  логотип:           {logo_key or 'нет'}")
    print(f"  по умолчанию:      {'да' if organization.is_default else 'нет'}")


async def _upload_logo(path: Path, account_id: uuid.UUID) -> str:
    """Кладёт логотип в объектное хранилище и возвращает его ключ.

    Файл читается синхронно: это разовая консольная команда, блокировать
    здесь нечего.
    """
    if not path.exists():  # noqa: ASYNC240
        raise SystemExit(f"Файл логотипа не найден: {path}")
    content_type = LOGO_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise SystemExit("Логотип должен быть в формате PNG или JPEG.")

    storage = S3Storage(get_settings())
    storage.ensure_bucket()
    key = f"logos/{account_id}/{uuid.uuid4().hex}{path.suffix.lower()}"
    await storage.put(
        key,
        path.read_bytes(),  # noqa: ASYNC240
        content_type=content_type,
    )
    return key


async def cmd_token_issue(args: argparse.Namespace) -> None:
    """Выпускает новый ключ API."""
    token = generate_token()
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        session.add(
            ApiToken(
                account_id=account.id,
                token_hash=hash_token(token),
                prefix=token_prefix(token),
                label=args.label,
            )
        )
        await session.commit()

    print("Ключ выпущен. Он показывается ОДИН раз — сохраните его сейчас:\n")
    print(f"  {token}\n")
    print("Пример вставки в шаблон письма:")
    print(
        f'  <img src="https://nosbp.ru/v1/qr?token={token}'
        '&sum=4700000&purpose=Заказ+1234" alt="QR для оплаты">'
    )


async def cmd_token_revoke(args: argparse.Namespace) -> None:
    """Отзывает ключ по его префиксу."""
    from nosbp.db.base import utcnow

    async with get_session_factory()() as session:
        result = await session.execute(
            select(ApiToken).where(ApiToken.prefix == args.prefix)
        )
        tokens = list(result.scalars())
        if not tokens:
            raise SystemExit(f"Ключ с префиксом {args.prefix} не найден.")
        for api_token in tokens:
            api_token.revoked_at = utcnow()
        await session.commit()
    print(f"Отозвано ключей: {len(tokens)}")


async def cmd_topup(args: argparse.Namespace) -> None:
    """Пополняет баланс заказчика."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        billing = BillingService(session, get_settings())
        locked = await billing.lock_account(account.id)
        await billing.topup(
            locked, args.rubles * 100, comment=args.comment or "Пополнение"
        )
        await session.commit()
        print(f"Баланс {account.email}: {locked.balance_kopecks / 100:.2f} ₽")


async def cmd_refund(args: argparse.Namespace) -> None:
    """Возвращает деньги на баланс заказчика."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        billing = BillingService(session, get_settings())
        locked = await billing.lock_account(account.id)
        await billing.refund(locked, args.rubles * 100, comment=args.comment)
        await session.commit()
        print(f"Баланс {account.email}: {locked.balance_kopecks / 100:.2f} ₽")


async def cmd_balance(args: argparse.Namespace) -> None:
    """Показывает баланс и последние операции."""
    async with get_session_factory()() as session:
        account = await _find_account(session, args.email)
        from_ledger = await calculate_balance_from_ledger(session, account.id)

        print(f"Аккаунт:  {account.email}")
        print(f"Баланс:   {account.balance_kopecks / 100:.2f} ₽")
        if from_ledger != account.balance_kopecks:
            print(
                f"ВНИМАНИЕ: журнал даёт {from_ledger / 100:.2f} ₽ — "
                "кэш баланса разошёлся с журналом."
            )
        if account.overdraft_until is not None:
            print(f"Овердрафт до: {account.overdraft_until:%d.%m.%Y %H:%M} UTC")

        entries = await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.account_id == account.id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(args.limit)
        )
        print(f"\nПоследние операции (до {args.limit}):")
        for entry in entries.scalars():
            print(
                f"  {entry.created_at:%d.%m.%Y %H:%M}  "
                f"{entry.amount_kopecks / 100:>10.2f} ₽  "
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
        help="Суточный потолок списаний в рублях. 0 — снять ограничение.",
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
    add.add_argument("--color", default="#000000", help="Цвет QR, #RRGGBB")
    add.add_argument("--logo", default=None, help="Путь к PNG или JPEG логотипа")
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
    revoke.add_argument("--prefix", required=True, help="Первые 8 символов ключа")
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
    balance.add_argument("--limit", type=int, default=20)
    balance.set_defaults(func=cmd_balance)

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
