"""HTTP-эндпоинты панели управления.

Страницы формируются на сервере из шаблонов Jinja2. Клиентский код
ограничен подсказками в формах и вынесен в статические файлы.
"""

import uuid
from enum import StrEnum
from typing import Annotated, Final
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin import crud
from nosbp.admin.dependencies import (
    AdminContext,
    CsrfProtected,
    CurrentAdmin,
    LogoCacheDep,
    Templates,
)
from nosbp.admin.logos import store_logo
from nosbp.admin.security import SESSION_COOKIE_NAME
from nosbp.admin.service import AdminAuthService
from nosbp.admin.stats import (
    collect_account_stats,
    collect_many_account_stats,
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
from nosbp.core.money import format_roubles, parse_roubles, roubles_input
from nosbp.db.base import utcnow
from nosbp.db.models import Account, ApiToken, LedgerEntry, Organization
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.storage.logo_cache import LogoCache

router = APIRouter(tags=["admin"], include_in_schema=False)
log = structlog.get_logger()

SEE_OTHER: Final = 303
"""Код ответа после успешной обработки формы.

Перенаправление после POST исключает повторное выполнение операции при
обновлении страницы."""

LEDGER_PAGE_SIZE: Final = 50

PERIOD_MONTH: Final[str] = "month"
"""Период по умолчанию — текущий календарный месяц."""


class BalanceOperation(StrEnum):
    """Что делает форма операций по балансу."""

    TOPUP = "topup"
    REFUND = "refund"
    ADJUST = "adjust"


EMPTY_ACCOUNT_FORM: Final[dict[str, str | bool]] = {
    "email": "",
    "display_name": "",
    "daily_limit": "",
    "is_unlimited": False,
}

EMPTY_BALANCE_FORM: Final[dict[str, str | bool]] = {
    "operation": BalanceOperation.TOPUP,
    "amount": "",
    "comment": "",
}


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


def _redirect(settings: Settings, path: str, **flash: str) -> RedirectResponse:
    """Перенаправляет внутрь панели, передав сообщение через адрес."""
    query = urlencode({key: value for key, value in flash.items() if value})
    target = f"{settings.admin_prefix}{path}"
    return RedirectResponse(f"{target}?{query}" if query else target, SEE_OTHER)


def _client_ip(request: Request) -> str:
    """Определяет адрес клиента с учётом обратного прокси.

    Заголовку X-Forwarded-For можно верить только потому, что снаружи
    сервис доступен исключительно через собственный прокси: он этот
    заголовок и проставляет, перетирая присланный клиентом. Значение
    используется в логах, не в проверках доступа.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _page(
    templates: Templates,
    request: Request,
    name: str,
    settings: Settings,
    *,
    admin: AdminContext | None = None,
    err: str = "",
    **context: object,
) -> Response:
    """Отрисовывает страницу, добавив общие для всех шаблонов значения.

    Токен CSRF добавляется в контекст автоматически, чтобы его нельзя
    было пропустить при добавлении нового шаблона.
    """
    return templates.TemplateResponse(
        request,
        name,
        {
            "settings": settings,
            "admin": admin,
            "csrf": admin.csrf_token if admin is not None else "",
            "ok": request.query_params.get("ok", ""),
            "err": err or request.query_params.get("err", ""),
            **context,
        },
    )


def _error_text(error: Exception) -> str:
    """Достаёт человеческий текст из доменной ошибки или из ValueError."""
    return error.message if isinstance(error, NosbpError) else str(error)


async def _reload(db: AsyncSession, *instances: object) -> None:
    """Перечитывает объекты после отката транзакции.

    Откат помечает все загруженные объекты устаревшими. Шаблон, дойдя
    до такого объекта, попытался бы сходить за ним в базу посреди
    отрисовки — а синхронный Jinja этого сделать не может и падает.
    Поэтому всё, что попадёт на страницу, перечитывается заранее.
    """
    for instance in instances:
        if instance is not None:
            await db.refresh(instance)


def _checkbox(value: str | None) -> bool:
    """Браузер присылает отмеченную галочку строкой, а снятую — ничем."""
    return value is not None


OrganizationFields = Annotated[str, Form()]
"""Поле формы организации: всё приходит строками, как их прислал браузер."""

FormValues = dict[str, str | bool]
"""Значения полей формы.

Шаблоны читают значения отсюда, а не из модели: при ошибке валидации
форма возвращается заполненной введёнными данными.
"""


def _organization_values(
    organization: Organization | None, settings: Settings
) -> FormValues:
    """Готовит значения формы организации: из модели или пустые."""
    if organization is None:
        return {
            "alias": "",
            "name": "",
            "personal_acc": "",
            "bank_name": "",
            "bic": "",
            "corresp_acc": "",
            "payee_inn": "",
            "kpp": "",
            "qr_color": DEFAULT_QR_COLOR,
            "fee_percent": _fee_input(settings.default_acquiring_fee_bps),
            "average_check": "",
            "is_default": False,
        }
    return {
        "alias": organization.alias,
        "name": organization.name,
        "personal_acc": organization.personal_acc,
        "bank_name": organization.bank_name,
        "bic": organization.bic,
        "corresp_acc": organization.corresp_acc,
        "payee_inn": organization.payee_inn,
        "kpp": organization.kpp or "",
        "qr_color": organization.qr_color,
        "fee_percent": _fee_input(organization.acquiring_fee_bps),
        "average_check": roubles_input(organization.average_check_kopecks),
        "is_default": organization.is_default,
    }


def _fee_input(bps: int) -> str:
    """Ставка для поля ввода: «0,7» без знака процента."""
    return format_fee_percent(bps).replace(" %", "")


def _submitted_values(
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
) -> FormValues:
    """Собирает то, что человек ввёл, — для повторной отрисовки формы."""
    return {
        "alias": alias,
        "name": name,
        "personal_acc": personal_acc,
        "bank_name": bank_name,
        "bic": bic,
        "corresp_acc": corresp_acc,
        "payee_inn": payee_inn,
        "kpp": kpp,
        "qr_color": qr_color,
        "fee_percent": fee_percent,
        "average_check": average_check,
        "is_default": _checkbox(is_default),
    }


def _organization_input(
    values: FormValues, default_fee_bps: int
) -> crud.RequisitesInput:
    """Переводит значения формы в проверяемый набор реквизитов.

    Ставка и средний чек разбираются здесь, а не схемой FastAPI: ошибка
    ввода должна превращаться в понятный текст, а не в стандартный ответ
    о непройденной валидации.

    :raises ValueError: если ставка или средний чек введены неверно.
    """
    fee_percent = str(values["fee_percent"]).strip()
    average_check = str(values["average_check"]).strip()

    return crud.RequisitesInput(
        alias=str(values["alias"]),
        name=str(values["name"]),
        personal_acc=str(values["personal_acc"]),
        bank_name=str(values["bank_name"]),
        bic=str(values["bic"]),
        corresp_acc=str(values["corresp_acc"]),
        payee_inn=str(values["payee_inn"]),
        kpp=str(values["kpp"]) or None,
        qr_color=str(values["qr_color"]),
        acquiring_fee_bps=(
            parse_fee_percent(fee_percent) if fee_percent else default_fee_bps
        ),
        average_check_kopecks=(parse_roubles(average_check) if average_check else None),
        is_default=bool(values["is_default"]),
    )


async def _attach_logo(
    organization: Organization,
    upload: UploadFile | None,
    settings: Settings,
    logo_cache: LogoCache,
) -> None:
    """Кладёт присланный логотип в хранилище и привязывает его к организации.

    Пустое поле файла браузер всё равно присылает — с пустым именем;
    такой «файл» игнорируется, иначе сохранение формы без выбора файла
    стирало бы прежний логотип.

    :raises ValidationError: если формат не поддерживается, файл слишком
        большой или это вовсе не картинка.
    """
    if upload is None or not upload.filename:
        return

    data = await upload.read()
    key = await store_logo(
        storage=logo_cache.storage,
        account_id=organization.account_id,
        filename=upload.filename,
        data=data,
        max_bytes=settings.logo_max_bytes,
    )

    previous = organization.logo_key
    organization.logo_key = key

    # Кэш держит логотип в памяти процесса; без сброса организация
    # ещё несколько минут показывала бы старую картинку.
    if previous is not None:
        logo_cache.invalidate(previous)


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
        path=settings.admin_prefix or "/",
    )
    return response


@router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    admin: CsrfProtected,
) -> RedirectResponse:
    """Закрывает сессию.

    Тоже под защитой токена: без неё чужая страница могла бы выкидывать
    администратора из панели на каждом заходе.
    """
    await AdminAuthService(db, settings).close_session(
        request.cookies.get(SESSION_COOKIE_NAME)
    )
    response = _redirect(settings, "/login", ok="Вы вышли из панели.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.admin_prefix or "/")
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
    period: str = PERIOD_MONTH,
) -> Response:
    """Список заказчиков с балансом и экономией."""
    since = month_start(utcnow()) if period == PERIOD_MONTH else None
    accounts = await crud.list_accounts(db, search=q)
    rows = await collect_many_account_stats(db, accounts, since=since)

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
        page_size=crud.ACCOUNTS_PAGE_SIZE,
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
        form=EMPTY_ACCOUNT_FORM,
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
    submitted: FormValues = {
        "email": email,
        "display_name": display_name,
        "daily_limit": daily_limit,
        "is_unlimited": _checkbox(is_unlimited),
    }

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
        return _page(
            templates,
            request,
            "account_form.html",
            settings,
            admin=admin,
            err=_error_text(error),
            form=submitted,
        )

    log.info("admin_account_created", admin=admin.email, account=account.email)
    return _redirect(
        settings, f"/accounts/{account.id}", ok=f"Заказчик {account.email} заведён."
    )


# ---------------------------------------------------------------------------
# Карточка заказчика
# ---------------------------------------------------------------------------


async def _render_account(
    *,
    request: Request,
    db: DbSession,
    settings: Settings,
    templates: Templates,
    admin: AdminContext,
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
    since = month_start(utcnow()) if period == PERIOD_MONTH else None

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
        err=err,
        account=account,
        stats=await collect_account_stats(db, account, since=since),
        ledger=list(ledger.scalars()),
        ledger_balance=await calculate_balance_from_ledger(db, account.id),
        tokens=await crud.list_tokens(db, account.id),
        period=period,
        issued_token=request.query_params.get("token", ""),
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
async def account_detail(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CurrentAdmin,
    account_id: uuid.UUID,
    period: str = PERIOD_MONTH,
) -> Response:
    """Карточка заказчика: организации, баланс, выписка, экономия."""
    account = await crud.get_account(db, account_id)
    return await _render_account(
        request=request,
        db=db,
        settings=settings,
        templates=templates,
        admin=admin,
        account=account,
        period=period,
    )


@router.post("/accounts/{account_id}/edit")
async def account_edit(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CsrfProtected,
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
        "is_active": _checkbox(is_active),
        "is_unlimited": _checkbox(is_unlimited),
    }

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
        await _reload(db, account)
        return await _render_account(
            request=request,
            db=db,
            settings=settings,
            templates=templates,
            admin=admin,
            account=account,
            period=PERIOD_MONTH,
            err=_error_text(error),
            edit=submitted,
        )

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
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CsrfProtected,
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
        text = comment.strip() or f"Операция администратора {admin.email}"

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
        await _reload(db, account)
        return await _render_account(
            request=request,
            db=db,
            settings=settings,
            templates=templates,
            admin=admin,
            account=account,
            period=PERIOD_MONTH,
            err=_error_text(error),
            balance=submitted,
        )

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
        account=account,
        organization=None,
        form=_organization_values(None, settings),
    )


@router.post("/accounts/{account_id}/organizations/new")
async def organization_create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CsrfProtected,
    account_id: uuid.UUID,
    alias: OrganizationFields,
    name: OrganizationFields,
    personal_acc: OrganizationFields,
    bank_name: OrganizationFields,
    bic: OrganizationFields,
    corresp_acc: OrganizationFields,
    payee_inn: OrganizationFields,
    logo_cache: LogoCacheDep,
    kpp: OrganizationFields = "",
    qr_color: OrganizationFields = DEFAULT_QR_COLOR,
    fee_percent: OrganizationFields = "",
    average_check: OrganizationFields = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Добавляет организацию."""
    account = await crud.get_account(db, account_id)
    submitted = _submitted_values(
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
    )

    try:
        data = _organization_input(submitted, settings.default_acquiring_fee_bps)
        organization = await crud.create_organization(db, account, data)
        await _attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await _reload(db, account)
        return _page(
            templates,
            request,
            "organization_form.html",
            settings,
            admin=admin,
            err=_error_text(error),
            account=account,
            organization=None,
            form=submitted,
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
        account=account,
        organization=organization,
        form=_organization_values(organization, settings),
    )


@router.post("/organizations/{organization_id}/edit")
async def organization_update(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    admin: CsrfProtected,
    organization_id: uuid.UUID,
    alias: OrganizationFields,
    name: OrganizationFields,
    personal_acc: OrganizationFields,
    bank_name: OrganizationFields,
    bic: OrganizationFields,
    corresp_acc: OrganizationFields,
    payee_inn: OrganizationFields,
    logo_cache: LogoCacheDep,
    kpp: OrganizationFields = "",
    qr_color: OrganizationFields = DEFAULT_QR_COLOR,
    fee_percent: OrganizationFields = "",
    average_check: OrganizationFields = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Сохраняет организацию."""
    organization = await _get_organization(db, organization_id)
    account = await crud.get_account(db, organization.account_id)
    submitted = _submitted_values(
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
    )

    try:
        data = _organization_input(submitted, settings.default_acquiring_fee_bps)
        await crud.update_organization(db, organization, data)
        await _attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await _reload(db, account, organization)
        return _page(
            templates,
            request,
            "organization_form.html",
            settings,
            admin=admin,
            err=_error_text(error),
            account=account,
            organization=organization,
            form=submitted,
        )

    log.info(
        "admin_organization_updated",
        admin=admin.email,
        organization=organization.alias,
    )
    return _redirect(settings, f"/accounts/{account.id}", ok="Организация сохранена.")


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
