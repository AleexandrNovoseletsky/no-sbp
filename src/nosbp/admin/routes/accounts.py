"""Заказчики: список, карточка, баланс, доступ в кабинет."""

import datetime
import uuid
from enum import StrEnum
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from nosbp.admin import crud
from nosbp.admin.dependencies import (
    AdminContext,
    CsrfProtected,
    CurrentAdmin,
    MailerDep,
    Templates,
)
from nosbp.admin.stats import (
    collect_account_stats,
    collect_many_account_stats,
    month_start,
)
from nosbp.billing.service import BillingService, calculate_balance_from_ledger
from nosbp.cabinet import access
from nosbp.core.config import Settings
from nosbp.core.errors import NosbpError, ValidationError
from nosbp.core.money import format_roubles, parse_roubles, roubles_input
from nosbp.db.base import utcnow
from nosbp.db.models import Account, LedgerEntry
from nosbp.invoices.service import list_recent_invoices
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import (
    FormValues,
    checkbox,
    error_text,
    page,
    redirect,
    reload_after_rollback,
)

router = APIRouter()
log = structlog.get_logger()

LEDGER_PAGE_SIZE: Final = 50
INVOICE_PAGE_SIZE: Final = 50
PERIOD_MONTH: Final[str] = "month"


class BalanceOperation(StrEnum):
    """Что делает форма операций по балансу."""

    TOPUP = "topup"
    REFUND = "refund"
    ADJUST = "adjust"


EMPTY_ACCOUNT_FORM: Final[FormValues] = {
    "email": "",
    "display_name": "",
    "daily_limit": "",
    "is_unlimited": False,
}

EMPTY_BALANCE_FORM: Final[FormValues] = {
    "operation": BalanceOperation.TOPUP,
    "amount": "",
    "comment": "",
}


def _since(period: str) -> datetime.datetime | None:
    """Начало периода отчёта."""
    return month_start(utcnow()) if period == PERIOD_MONTH else None


# ---------------------------------------------------------------------------
# Список
# ---------------------------------------------------------------------------


@router.get("/")
async def index(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAdmin,
    q: str = "",
    period: str = PERIOD_MONTH,
) -> Response:
    """Список заказчиков с балансом и экономией."""
    accounts = await crud.list_accounts(db, search=q)
    rows = await collect_many_account_stats(db, accounts, since=_since(period))

    return page(
        templates,
        request,
        "accounts.html",
        settings,
        viewer=viewer,
        rows=rows,
        search=q,
        period=period,
        total_savings=sum(row.savings_kopecks for row in rows),
        total_invoices=sum(row.invoice_count for row in rows),
        page_size=crud.ACCOUNTS_PAGE_SIZE,
    )


@router.get("/accounts/new")
async def new_form(
    request: Request, templates: Templates, settings: AppSettings, viewer: CurrentAdmin
) -> Response:
    """Форма создания заказчика."""
    return page(
        templates,
        request,
        "account_form.html",
        settings,
        viewer=viewer,
        form=EMPTY_ACCOUNT_FORM,
    )


@router.post("/accounts/new")
async def create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    is_unlimited: Annotated[str | None, Form()] = None,
    daily_limit: Annotated[str, Form()] = "",
) -> Response:
    """Заводит заказчика."""
    submitted: FormValues = {
        "email": email,
        "display_name": display_name,
        "daily_limit": daily_limit,
        "is_unlimited": checkbox(is_unlimited),
    }

    try:
        limit = parse_roubles(daily_limit) if daily_limit.strip() else None
        account = await crud.create_account(
            db,
            settings,
            email=email,
            display_name=display_name,
            daily_limit_kopecks=limit,
            is_unlimited=checkbox(is_unlimited),
            use_default_limit=not daily_limit.strip(),
        )
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        return page(
            templates,
            request,
            "account_form.html",
            settings,
            viewer=viewer,
            err=error_text(error),
            form=submitted,
        )

    log.info("admin_account_created", admin=viewer.email, account=account.email)
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account.id}",
        ok=(
            f"Заказчик {account.email} заведён. Выдайте ссылку для входа "
            "в кабинет — пароля у него пока нет."
        ),
    )


# ---------------------------------------------------------------------------
# Карточка
# ---------------------------------------------------------------------------


async def _render(
    *,
    request: Request,
    db: DbSession,
    settings: Settings,
    templates: Templates,
    viewer: AdminContext,
    account: Account,
    period: str,
    err: str = "",
    edit: FormValues | None = None,
    balance: FormValues | None = None,
) -> Response:
    """Отрисовывает карточку заказчика.

    Значения форм передаются отдельно от модели: после ошибки страница
    возвращается с тем, что человек ввёл, а не с тем, что лежит в базе.
    """
    ledger = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == account.id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(LEDGER_PAGE_SIZE)
    )

    return page(
        templates,
        request,
        "account.html",
        settings,
        viewer=viewer,
        err=err,
        account=account,
        stats=await collect_account_stats(db, account, since=_since(period)),
        ledger=list(ledger.scalars()),
        ledger_balance=await calculate_balance_from_ledger(db, account.id),
        tokens=await crud.list_tokens(db, account.id),
        invoices=await list_recent_invoices(db, account.id, limit=INVOICE_PAGE_SIZE),
        period=period,
        issued_token=request.query_params.get("token", ""),
        invite_url=request.query_params.get("invite", ""),
        edit=edit or _account_values(account),
        balance=balance or EMPTY_BALANCE_FORM,
    )


def _account_values(account: Account) -> FormValues:
    """Значения формы карточки заказчика."""
    return {
        "display_name": account.display_name,
        "daily_limit": roubles_input(account.daily_charge_limit_kopecks),
        "is_active": account.is_active,
        "is_unlimited": account.is_unlimited,
    }


@router.get("/accounts/{account_id}")
async def detail(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAdmin,
    account_id: uuid.UUID,
    period: str = PERIOD_MONTH,
) -> Response:
    """Карточка заказчика."""
    return await _render(
        request=request,
        db=db,
        settings=settings,
        templates=templates,
        viewer=viewer,
        account=await crud.get_account(db, account_id),
        period=period,
    )


@router.post("/accounts/{account_id}/edit")
async def edit(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    account_id: uuid.UUID,
    display_name: Annotated[str, Form()],
    is_active: Annotated[str | None, Form()] = None,
    is_unlimited: Annotated[str | None, Form()] = None,
    daily_limit: Annotated[str, Form()] = "",
) -> Response:
    """Сохраняет карточку заказчика."""
    account = await crud.get_account(db, account_id)
    submitted: FormValues = {
        "display_name": display_name,
        "daily_limit": daily_limit,
        "is_active": checkbox(is_active),
        "is_unlimited": checkbox(is_unlimited),
    }

    try:
        limit = parse_roubles(daily_limit) if daily_limit.strip() else None
        await crud.update_account(
            account,
            display_name=display_name,
            is_active=checkbox(is_active),
            is_unlimited=checkbox(is_unlimited),
            daily_limit_kopecks=limit,
        )
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await reload_after_rollback(db, account)
        return await _render(
            request=request,
            db=db,
            settings=settings,
            templates=templates,
            viewer=viewer,
            account=account,
            period=PERIOD_MONTH,
            err=error_text(error),
            edit=submitted,
        )

    log.info("admin_account_updated", admin=viewer.email, account=account.email)
    return redirect(
        settings.admin_prefix, f"/accounts/{account_id}", ok="Карточка сохранена."
    )


@router.post("/accounts/{account_id}/delete")
async def delete(
    db: DbSession, settings: AppSettings, viewer: CsrfProtected, account_id: uuid.UUID
) -> RedirectResponse:
    """Удаляет заказчика со всеми его данными."""
    account = await crud.get_account(db, account_id)
    email = account.email

    await crud.delete_account(db, account)
    await db.commit()

    log.warning("admin_account_deleted", admin=viewer.email, account=email)
    return redirect(settings.admin_prefix, "/", ok=f"Заказчик {email} удалён.")


# ---------------------------------------------------------------------------
# Доступ в кабинет
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/invite")
async def invite(
    db: DbSession,
    settings: AppSettings,
    mailer: MailerDep,
    viewer: CsrfProtected,
    account_id: uuid.UUID,
) -> RedirectResponse:
    """Отправляет заказчику ссылку для установки пароля.

    Нужна и для аккаунтов, заведённых оператором (пароля у них нет),
    и когда заказчик не справился с восстановлением сам. Прежние
    выданные ссылки при этом гасятся.

    Письмо отправляется сразу, а не фоновой задачей: оператор должен
    увидеть, дошло оно или нет. Ссылка в любом случае показывается
    на странице — её можно передать заказчику другим каналом.
    """
    account = await crud.get_account(db, account_id)
    delivery = await access.prepare_access_invite(db, settings, account)
    await db.commit()

    delivered = await mailer.send(delivery.to, delivery.letter)
    log.info(
        "admin_invite_issued",
        admin=viewer.email,
        account=account.email,
        delivered=delivered,
    )

    lifetime = f"она одноразовая и действует {settings.cabinet_invite_ttl_hours} ч"
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account_id}",
        invite=delivery.url,
        ok=(
            f"Письмо отправлено на {account.email} — {lifetime}."
            if delivered
            else f"Письмо отправить не удалось: передайте ссылку сами, {lifetime}."
        ),
    )


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/balance")
async def balance_operation(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    account_id: uuid.UUID,
    operation: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
) -> Response:
    """Пополняет, возвращает или корректирует баланс."""
    account = await crud.get_account(db, account_id)
    billing = BillingService(db, settings)
    submitted: FormValues = {
        "operation": operation,
        "amount": amount,
        "comment": comment,
    }

    try:
        kopecks = parse_roubles(amount)
        locked = await billing.lock_account(account.id)
        text = comment.strip() or f"Операция администратора {viewer.email}"

        if operation == BalanceOperation.TOPUP:
            await billing.topup(locked, kopecks, comment=text)
        elif operation == BalanceOperation.REFUND:
            await billing.refund(locked, kopecks, comment=text)
        elif operation == BalanceOperation.ADJUST:
            await billing.adjust(locked, kopecks, comment=text)
        else:
            raise ValidationError(f"Неизвестная операция: {operation}")
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await reload_after_rollback(db, account)
        return await _render(
            request=request,
            db=db,
            settings=settings,
            templates=templates,
            viewer=viewer,
            account=account,
            period=PERIOD_MONTH,
            err=error_text(error),
            balance=submitted,
        )

    log.info(
        "admin_balance_operation",
        admin=viewer.email,
        account=account.email,
        operation=operation,
        kopecks=kopecks,
    )
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account_id}",
        ok=f"Баланс: {format_roubles(locked.balance_kopecks)}.",
    )
