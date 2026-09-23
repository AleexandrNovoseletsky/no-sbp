"""Обзор, статистика и баланс."""

import datetime
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from nosbp.admin import crud
from nosbp.admin.stats import AccountStats, collect_account_stats, month_start
from nosbp.billing.service import calculate_balance_from_ledger
from nosbp.cabinet.dependencies import CsrfProtected, CurrentAccount, Templates
from nosbp.cabinet.service import SESSION_COOKIE_NAME, CabinetAuthService
from nosbp.core.errors import NosbpError, ValidationError
from nosbp.db.base import utcnow
from nosbp.db.models import LedgerEntry
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import error_text, page, redirect

router = APIRouter()
log = structlog.get_logger()

LEDGER_PAGE_SIZE: Final = 50
INVOICE_PAGE_SIZE: Final = 50

PERIOD_MONTH: Final[str] = "month"
DEFAULT_ALIAS: Final[str] = "main"
"""Заглушка в примере интеграции, пока организаций нет."""


def _since(period: str) -> datetime.datetime | None:
    """Начало периода отчёта: текущий месяц или всё время."""
    return month_start(utcnow()) if period == PERIOD_MONTH else None


def _snippet_alias(stats: AccountStats) -> str:
    """Алиас организации для примера интеграции.

    Берётся из уже загруженной статистики, а не отдельным запросом.
    """
    if not stats.organizations:
        return DEFAULT_ALIAS
    return stats.organizations[0].organization.alias


@router.get("/")
async def dashboard(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAccount,
    period: str = PERIOD_MONTH,
) -> Response:
    """Обзор: показатели, организации, ключи, пример интеграции."""
    stats = await collect_account_stats(db, viewer.account, since=_since(period))
    return page(
        templates,
        request,
        "dashboard.html",
        settings,
        viewer=viewer,
        account=viewer.account,
        stats=stats,
        tokens=await crud.list_tokens(db, viewer.account.id),
        period=period,
        issued_token=request.query_params.get("token", ""),
        organization_alias=_snippet_alias(stats),
    )


@router.get("/statistics")
async def statistics(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAccount,
    period: str = PERIOD_MONTH,
) -> Response:
    """Статистика по организациям и история счетов."""
    from nosbp.invoices.service import list_recent_invoices

    return page(
        templates,
        request,
        "statistics.html",
        settings,
        viewer=viewer,
        account=viewer.account,
        stats=await collect_account_stats(db, viewer.account, since=_since(period)),
        invoices=await list_recent_invoices(
            db, viewer.account.id, limit=INVOICE_PAGE_SIZE
        ),
        period=period,
    )


@router.get("/balance")
async def balance(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAccount,
) -> Response:
    """Баланс и выписка по операциям."""
    ledger = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == viewer.account.id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(LEDGER_PAGE_SIZE)
    )
    return page(
        templates,
        request,
        "balance.html",
        settings,
        viewer=viewer,
        account=viewer.account,
        ledger=list(ledger.scalars()),
        ledger_balance=await calculate_balance_from_ledger(db, viewer.account.id),
    )


@router.post("/password")
async def change_password(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    new_password_repeat: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Меняет пароль и закрывает все открытые сессии."""
    service = CabinetAuthService(db, settings)
    try:
        if new_password != new_password_repeat:
            raise ValidationError("Новые пароли не совпадают.")
        await service.change_password(
            viewer.account, current=current_password, new=new_password
        )
    except NosbpError as error:
        await db.rollback()
        return redirect(settings.cabinet_prefix, "/balance", err=error_text(error))

    log.info("cabinet_password_changed", account=viewer.email)
    response = redirect(
        settings.cabinet_prefix, "/login", ok="Пароль изменён. Войдите заново."
    )
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.cabinet_prefix or "/")
    return response
