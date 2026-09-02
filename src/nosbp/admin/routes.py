"""Страницы панели управления.

Обычные серверные формы без сборщика и без JavaScript: панелью пользуется
один человек, и любая машинерия сверх этого была бы платой без выгоды.
"""

import uuid
from typing import Annotated, Final
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from nosbp.admin import crud
from nosbp.admin.dependencies import CsrfProtected, CurrentAdmin, Templates
from nosbp.admin.security import SESSION_COOKIE_NAME
from nosbp.admin.service import AdminAuthService
from nosbp.admin.stats import (
    collect_account_stats,
    format_fee_percent,
    month_start,
    parse_fee_percent,
)
from nosbp.billing.service import BillingService, calculate_balance_from_ledger
from nosbp.core.config import Settings
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.errors import (
    AdminAuthError,
    AdminLockedError,
    NosbpError,
    ValidationError,
)
from nosbp.core.money import format_roubles, parse_roubles
from nosbp.db.base import utcnow
from nosbp.db.models import ApiToken, LedgerEntry, Organization
from nosbp.payments.dependencies import AppSettings, DbSession

router = APIRouter(tags=["admin"], include_in_schema=False)
log = structlog.get_logger()

SEE_OTHER: Final = 303
"""После POST положено отвечать редиректом, иначе обновление страницы
повторяет операцию — а операции здесь денежные."""

LEDGER_PAGE_SIZE: Final = 50
UNLIMITED_LABEL: Final[str] = "без ограничения"


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


def _prefix(settings: Settings) -> str:
    return settings.admin_path_prefix.rstrip("/")


def _redirect(settings: Settings, path: str, **flash: str) -> RedirectResponse:
    """Перенаправляет внутрь панели, передав сообщение через адрес."""
    query = urlencode({key: value for key, value in flash.items() if value})
    target = f"{_prefix(settings)}{path}"
    return RedirectResponse(f"{target}?{query}" if query else target, SEE_OTHER)


def _client_ip(request: Request) -> str:
    """Определяет адрес клиента с учётом обратного прокси."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _page(
    templates: Templates,
    request: Request,
    name: str,
    settings: Settings,
    **context: object,
) -> Response:
    """Отрисовывает страницу, добавив общие для всех шаблонов значения."""
    return templates.TemplateResponse(
        request,
        name,
        {
            "settings": settings,
            "ok": request.query_params.get("ok", ""),
            "err": request.query_params.get("err", ""),
            "format_roubles": format_roubles,
            "format_fee_percent": format_fee_percent,
            **context,
        },
    )


def _checkbox(value: str | None) -> bool:
    """Браузер присылает отмеченную галочку строкой, а снятую — ничем."""
    return value is not None


OrganizationFields = Annotated[str, Form()]
"""Поле формы организации: всё приходит строками, как их прислал браузер."""


def _organization_input(
    *,
    alias: str,
    name: str,
    personal_acc: str,
    bank_name: str,
    bic: str,
    corresp_acc: str,
    payee_inn: str,
    kpp: str,
    qr_color: str,
    fee_percent: str,
    average_check: str,
    is_default: str | None,
    default_fee_bps: int,
) -> crud.RequisitesInput:
    """Собирает из полей формы проверяемый набор реквизитов.

    Ставка и средний чек разбираются здесь, а не схемой FastAPI: ошибка
    ввода должна превращаться в понятный текст, а не в стандартный ответ
    о непройденной валидации.

    :raises ValueError: если ставка или средний чек введены неверно.
    """
    return crud.RequisitesInput(
        alias=alias,
        name=name,
        personal_acc=personal_acc,
        bank_name=bank_name,
        bic=bic,
        corresp_acc=corresp_acc,
        payee_inn=payee_inn,
        kpp=kpp or None,
        qr_color=qr_color,
        acquiring_fee_bps=(
            parse_fee_percent(fee_percent) if fee_percent.strip() else default_fee_bps
        ),
        average_check_kopecks=(
            parse_roubles(average_check) if average_check.strip() else None
        ),
        is_default=_checkbox(is_default),
    )


@router.get("/login")
async def login_form(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Страница входа."""
    return _page(templates, request, "login.html", settings)


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    totp_code: Annotated[str, Form()] = "",
) -> Response:
    """Проверяет вход и открывает сессию."""
    service = AdminAuthService(db, settings)
    try:
        admin = await service.authenticate(
            email=email, password=password, totp_code=totp_code
        )
    except (AdminAuthError, AdminLockedError) as error:
        log.warning("admin_login_failed", email=email, ip=_client_ip(request))
        return _page(
            templates, request, "login.html", settings, err=error.message, email=email
        )

    issued = await service.open_session(
        admin,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    log.info("admin_login", admin=admin.email, ip=_client_ip(request))

    response = _redirect(settings, "/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=settings.admin_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path=_prefix(settings) or "/",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request, db: DbSession, settings: AppSettings
) -> RedirectResponse:
    """Закрывает сессию."""
    await AdminAuthService(db, settings).close_session(
        request.cookies.get(SESSION_COOKIE_NAME)
    )
    response = _redirect(settings, "/login", ok="Вы вышли из панели.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=_prefix(settings) or "/")
    return response


# ---------------------------------------------------------------------------
# Список заказчиков
# ---------------------------------------------------------------------------


@router.get("/")
async def accounts_index(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CurrentAdmin,
    q: str = "",
    period: str = "month",
) -> Response:
    """Список заказчиков с балансом и экономией."""
    since = month_start(utcnow()) if period == "month" else None
    accounts = await crud.list_accounts(db, search=q)

    rows = [
        await collect_account_stats(db, account, since=since) for account in accounts
    ]

    return _page(
        templates,
        request,
        "accounts.html",
        settings,
        admin=admin,
        rows=rows,
        search=q,
        period=period,
        total_savings=sum(row.savings_kopecks for row in rows),
        total_invoices=sum(row.invoice_count for row in rows),
    )


@router.get("/accounts/new")
async def account_new_form(
    request: Request, templates: Templates, settings: AppSettings, admin: CurrentAdmin
) -> Response:
    """Форма создания заказчика."""
    return _page(
        templates,
        request,
        "account_form.html",
        settings,
        admin=admin,
        csrf=admin.csrf_token,
    )


@router.post("/accounts/new")
async def account_create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CsrfProtected,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    is_unlimited: Annotated[str | None, Form()] = None,
    daily_limit: Annotated[str, Form()] = "",
) -> Response:
    """Заводит заказчика."""
    try:
        limit = parse_roubles(daily_limit) if daily_limit.strip() else None
        account = await crud.create_account(
            db,
            settings,
            email=email,
            display_name=display_name,
            daily_limit_kopecks=limit,
            is_unlimited=_checkbox(is_unlimited),
            use_default_limit=not daily_limit.strip(),
        )
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        message = error.message if isinstance(error, NosbpError) else str(error)
        return _page(
            templates,
            request,
            "account_form.html",
            settings,
            admin=admin,
            csrf=admin.csrf_token,
            err=message,
            email=email,
            display_name=display_name,
        )

    log.info("admin_account_created", admin=admin.email, account=account.email)
    return _redirect(
        settings, f"/accounts/{account.id}", ok=f"Заказчик {account.email} заведён."
    )


# ---------------------------------------------------------------------------
# Карточка заказчика
# ---------------------------------------------------------------------------


@router.get("/accounts/{account_id}")
async def account_detail(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CurrentAdmin,
    account_id: uuid.UUID,
    period: str = "month",
) -> Response:
    """Карточка заказчика: организации, баланс, выписка, экономия."""
    account = await crud.get_account(db, account_id)
    since = month_start(utcnow()) if period == "month" else None

    ledger = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == account.id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(LEDGER_PAGE_SIZE)
    )

    return _page(
        templates,
        request,
        "account.html",
        settings,
        admin=admin,
        csrf=admin.csrf_token,
        account=account,
        stats=await collect_account_stats(db, account, since=since),
        ledger=list(ledger.scalars()),
        ledger_balance=await calculate_balance_from_ledger(db, account.id),
        tokens=await crud.list_tokens(db, account.id),
        period=period,
        issued_token=request.query_params.get("token", ""),
    )


@router.post("/accounts/{account_id}/edit")
async def account_edit(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    display_name: Annotated[str, Form()],
    is_active: Annotated[str | None, Form()] = None,
    is_unlimited: Annotated[str | None, Form()] = None,
    daily_limit: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Сохраняет карточку заказчика."""
    account = await crud.get_account(db, account_id)
    try:
        limit = parse_roubles(daily_limit) if daily_limit.strip() else None
        await crud.update_account(
            account,
            display_name=display_name,
            is_active=_checkbox(is_active),
            is_unlimited=_checkbox(is_unlimited),
            daily_limit_kopecks=limit,
        )
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        message = error.message if isinstance(error, NosbpError) else str(error)
        return _redirect(settings, f"/accounts/{account_id}", err=message)

    log.info("admin_account_updated", admin=admin.email, account=account.email)
    return _redirect(settings, f"/accounts/{account_id}", ok="Карточка сохранена.")


@router.post("/accounts/{account_id}/delete")
async def account_delete(
    db: DbSession, settings: AppSettings, admin: CsrfProtected, account_id: uuid.UUID
) -> RedirectResponse:
    """Удаляет заказчика со всеми его данными."""
    account = await crud.get_account(db, account_id)
    email = account.email
    await crud.delete_account(db, account)
    await db.commit()

    log.warning("admin_account_deleted", admin=admin.email, account=email)
    return _redirect(settings, "/", ok=f"Заказчик {email} удалён.")


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/balance")
async def balance_operation(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    operation: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    comment: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Пополняет, возвращает или корректирует баланс."""
    account = await crud.get_account(db, account_id)
    billing = BillingService(db, settings)

    try:
        kopecks = parse_roubles(amount)
        locked = await billing.lock_account(account.id)
        text = comment.strip() or f"Операция администратора {admin.email}"

        if operation == "topup":
            await billing.topup(locked, kopecks, comment=text)
        elif operation == "refund":
            await billing.refund(locked, kopecks, comment=text)
        elif operation == "adjust":
            await billing.adjust(locked, kopecks, comment=text)
        else:
            raise ValueError(f"Неизвестная операция: {operation}")
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        message = error.message if isinstance(error, NosbpError) else str(error)
        return _redirect(settings, f"/accounts/{account_id}", err=message)

    log.info(
        "admin_balance_operation",
        admin=admin.email,
        account=account.email,
        operation=operation,
        kopecks=kopecks,
    )
    return _redirect(
        settings,
        f"/accounts/{account_id}",
        ok=f"Баланс: {format_roubles(locked.balance_kopecks)}.",
    )


# ---------------------------------------------------------------------------
# Ключи API
# ---------------------------------------------------------------------------


@router.post("/accounts/{account_id}/tokens")
async def token_issue(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    label: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Выпускает ключ и показывает его один раз."""
    account = await crud.get_account(db, account_id)
    token = await crud.issue_token(db, account, label=label)
    await db.commit()

    log.info("admin_token_issued", admin=admin.email, account=account.email)
    return _redirect(
        settings,
        f"/accounts/{account_id}",
        token=token,
        ok="Ключ выпущен. Он показывается один раз — скопируйте его сейчас.",
    )


@router.post("/accounts/{account_id}/tokens/{token_id}/revoke")
async def token_revoke(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    token_id: uuid.UUID,
) -> RedirectResponse:
    """Отзывает ключ."""
    api_token = await db.get(ApiToken, token_id)
    if api_token is not None and api_token.revoked_at is None:
        api_token.revoked_at = utcnow()
        await db.commit()
        log.warning("admin_token_revoked", admin=admin.email, prefix=api_token.prefix)
    return _redirect(settings, f"/accounts/{account_id}", ok="Ключ отозван.")


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


@router.get("/accounts/{account_id}/organizations/new")
async def organization_new_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CurrentAdmin,
    account_id: uuid.UUID,
) -> Response:
    """Форма добавления организации."""
    account = await crud.get_account(db, account_id)
    return _page(
        templates,
        request,
        "organization_form.html",
        settings,
        admin=admin,
        csrf=admin.csrf_token,
        account=account,
        organization=None,
        default_fee_percent=format_fee_percent(settings.default_acquiring_fee_bps),
    )


@router.post("/accounts/{account_id}/organizations/new")
async def organization_create(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    alias: OrganizationFields,
    name: OrganizationFields,
    personal_acc: OrganizationFields,
    bank_name: OrganizationFields,
    bic: OrganizationFields,
    corresp_acc: OrganizationFields,
    payee_inn: OrganizationFields,
    kpp: OrganizationFields = "",
    qr_color: OrganizationFields = DEFAULT_QR_COLOR,
    fee_percent: OrganizationFields = "",
    average_check: OrganizationFields = "",
    is_default: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Добавляет организацию."""
    account = await crud.get_account(db, account_id)
    try:
        data = _organization_input(
            alias=alias,
            name=name,
            personal_acc=personal_acc,
            bank_name=bank_name,
            bic=bic,
            corresp_acc=corresp_acc,
            payee_inn=payee_inn,
            kpp=kpp,
            qr_color=qr_color,
            fee_percent=fee_percent,
            average_check=average_check,
            is_default=is_default,
            default_fee_bps=settings.default_acquiring_fee_bps,
        )
        organization = await crud.create_organization(db, account, data)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        message = error.message if isinstance(error, NosbpError) else str(error)
        return _redirect(
            settings, f"/accounts/{account_id}/organizations/new", err=message
        )

    log.info(
        "admin_organization_created",
        admin=admin.email,
        organization=organization.alias,
    )
    return _redirect(
        settings,
        f"/accounts/{account_id}",
        ok=f"Организация «{organization.alias}» добавлена.",
    )


@router.get("/organizations/{organization_id}")
async def organization_edit_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CurrentAdmin,
    organization_id: uuid.UUID,
) -> Response:
    """Форма изменения организации."""
    organization = await _get_organization(db, organization_id)
    account = await crud.get_account(db, organization.account_id)
    return _page(
        templates,
        request,
        "organization_form.html",
        settings,
        admin=admin,
        csrf=admin.csrf_token,
        account=account,
        organization=organization,
        default_fee_percent=format_fee_percent(settings.default_acquiring_fee_bps),
    )


@router.post("/organizations/{organization_id}/edit")
async def organization_update(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    organization_id: uuid.UUID,
    alias: OrganizationFields,
    name: OrganizationFields,
    personal_acc: OrganizationFields,
    bank_name: OrganizationFields,
    bic: OrganizationFields,
    corresp_acc: OrganizationFields,
    payee_inn: OrganizationFields,
    kpp: OrganizationFields = "",
    qr_color: OrganizationFields = DEFAULT_QR_COLOR,
    fee_percent: OrganizationFields = "",
    average_check: OrganizationFields = "",
    is_default: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Сохраняет организацию."""
    organization = await _get_organization(db, organization_id)
    account_id = organization.account_id
    try:
        data = _organization_input(
            alias=alias,
            name=name,
            personal_acc=personal_acc,
            bank_name=bank_name,
            bic=bic,
            corresp_acc=corresp_acc,
            payee_inn=payee_inn,
            kpp=kpp,
            qr_color=qr_color,
            fee_percent=fee_percent,
            average_check=average_check,
            is_default=is_default,
            default_fee_bps=settings.default_acquiring_fee_bps,
        )
        await crud.update_organization(db, organization, data)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        message = error.message if isinstance(error, NosbpError) else str(error)
        return _redirect(settings, f"/organizations/{organization_id}", err=message)

    log.info(
        "admin_organization_updated",
        admin=admin.email,
        organization=organization.alias,
    )
    return _redirect(settings, f"/accounts/{account_id}", ok="Организация сохранена.")


@router.post("/organizations/{organization_id}/delete")
async def organization_delete(
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
    organization_id: uuid.UUID,
) -> RedirectResponse:
    """Удаляет организацию вместе с её счетами."""
    organization = await _get_organization(db, organization_id)
    account_id = organization.account_id
    alias = organization.alias

    await crud.delete_organization(db, organization)
    await db.commit()

    log.warning("admin_organization_deleted", admin=admin.email, organization=alias)
    return _redirect(
        settings, f"/accounts/{account_id}", ok=f"Организация «{alias}» удалена."
    )


async def _get_organization(db: DbSession, organization_id: uuid.UUID) -> Organization:
    """Находит организацию или сообщает, что её нет."""
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise ValidationError("Организация не найдена.")
    return organization
