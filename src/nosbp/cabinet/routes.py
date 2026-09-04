"""HTTP-эндпоинты личного кабинета заказчика.

Все запросы к данным фильтруются по идентификатору вошедшего заказчика:
организация, ключ или счёт чужого аккаунта недоступны даже при подстановке
идентификатора в адрес.
"""

import uuid
from typing import Annotated, Final
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin import crud
from nosbp.admin.logos import store_logo
from nosbp.admin.stats import collect_account_stats, format_fee_percent, month_start
from nosbp.billing.service import calculate_balance_from_ledger
from nosbp.cabinet.dependencies import (
    CabinetContext,
    CsrfProtected,
    CurrentAccount,
    Templates,
)
from nosbp.cabinet.service import SESSION_COOKIE_NAME, CabinetAuthService
from nosbp.core.config import Settings
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.errors import (
    AccountDisabledError,
    CabinetAuthError,
    CabinetLockedError,
    NosbpError,
    OrganizationNotFoundError,
    ValidationError,
)
from nosbp.core.middleware import client_address
from nosbp.core.money import parse_roubles, roubles_input
from nosbp.db.base import utcnow
from nosbp.db.models import ApiToken, LedgerEntry, Organization
from nosbp.invoices.service import list_recent_invoices
from nosbp.payments.dependencies import AppSettings, DbSession, LogoCacheDep
from nosbp.storage.logo_cache import LogoCache

router = APIRouter(tags=["cabinet"], include_in_schema=False)
log = structlog.get_logger()

SEE_OTHER: Final = 303
SECONDS_PER_HOUR: Final = 60 * 60

LEDGER_PAGE_SIZE: Final = 50
INVOICE_PAGE_SIZE: Final = 50

PERIOD_MONTH: Final[str] = "month"

FormValues = dict[str, str | bool]

EMPTY_REGISTER_FORM: Final[FormValues] = {"email": "", "display_name": ""}


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


def _redirect(settings: Settings, path: str, **flash: str) -> RedirectResponse:
    """Перенаправляет внутрь кабинета, передав сообщение через адрес."""
    query = urlencode({key: value for key, value in flash.items() if value})
    target = f"{settings.cabinet_prefix}{path}"
    return RedirectResponse(f"{target}?{query}" if query else target, SEE_OTHER)


def _page(
    templates: Templates,
    request: Request,
    name: str,
    settings: Settings,
    *,
    account: CabinetContext | None = None,
    err: str = "",
    **context: object,
) -> Response:
    """Отрисовывает страницу кабинета."""
    return templates.TemplateResponse(
        request,
        name,
        {
            "settings": settings,
            "account": account,
            "csrf": account.csrf_token if account is not None else "",
            "ok": request.query_params.get("ok", ""),
            "err": err or request.query_params.get("err", ""),
            **context,
        },
    )


def _error_text(error: Exception) -> str:
    """Достаёт человеческий текст из доменной ошибки или из ValueError."""
    return error.message if isinstance(error, NosbpError) else str(error)


async def _reload(db: AsyncSession, *instances: object) -> None:
    """Перечитывает объекты после отката транзакции."""
    for instance in instances:
        if instance is not None:
            await db.refresh(instance)


def _checkbox(value: str | None) -> bool:
    """Браузер присылает отмеченную галочку строкой, а снятую — ничем."""
    return value is not None


async def _own_organization(
    db: AsyncSession, account_id: uuid.UUID, organization_id: uuid.UUID
) -> Organization:
    """Находит организацию, принадлежащую этому заказчику.

    Фильтр по владельцу обязателен: без него подстановка чужого
    идентификатора в адрес открыла бы доступ к чужим реквизитам.

    :raises OrganizationNotFoundError: организации нет либо она принадлежит
        другому заказчику. Ответ в обоих случаях одинаков.
    """
    result = await db.execute(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.account_id == account_id,
        )
    )
    organization = result.scalar_one_or_none()
    if organization is None:
        raise OrganizationNotFoundError("Организация не найдена.")
    return organization


# ---------------------------------------------------------------------------
# Регистрация и вход
# ---------------------------------------------------------------------------


@router.get("/register")
async def register_form(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Страница регистрации."""
    return _page(
        templates, request, "register.html", settings, form=EMPTY_REGISTER_FORM
    )


@router.post("/register")
async def register(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    """Создаёт учётную запись и сразу выполняет вход."""
    submitted: FormValues = {"email": email, "display_name": display_name}
    service = CabinetAuthService(db, settings)

    try:
        if password != password_repeat:
            raise ValidationError("Пароли не совпадают.")
        account = await service.register(
            email=email, password=password, display_name=display_name
        )
        await db.commit()
    except NosbpError as error:
        await db.rollback()
        return _page(
            templates,
            request,
            "register.html",
            settings,
            err=_error_text(error),
            form=submitted,
        )

    log.info("cabinet_registered", account=account.email)
    issued = await service.open_session(
        account,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    response = _redirect(settings, "/", ok="Аккаунт создан. Добавьте организацию.")
    _set_session_cookie(response, issued.token, settings)
    return response


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
) -> Response:
    """Проверяет учётные данные и открывает сессию."""
    service = CabinetAuthService(db, settings)
    try:
        account = await service.authenticate(email=email, password=password)
    except (CabinetAuthError, CabinetLockedError, AccountDisabledError) as error:
        log.warning(
            "cabinet_login_failed", email=email, address=client_address(request)
        )
        return _page(
            templates, request, "login.html", settings, err=error.message, email=email
        )

    issued = await service.open_session(
        account,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    log.info("cabinet_login", account=account.email)

    response = _redirect(settings, "/")
    _set_session_cookie(response, issued.token, settings)
    return response


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Проставляет сессионную куку кабинета."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.cabinet_session_ttl_hours * SECONDS_PER_HOUR,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path=settings.cabinet_prefix or "/",
    )


@router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    account: CsrfProtected,
) -> RedirectResponse:
    """Закрывает сессию."""
    await CabinetAuthService(db, settings).close_session(
        request.cookies.get(SESSION_COOKIE_NAME)
    )
    response = _redirect(settings, "/login", ok="Вы вышли из кабинета.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.cabinet_prefix or "/")
    return response


# ---------------------------------------------------------------------------
# Обзор
# ---------------------------------------------------------------------------


@router.get("/")
async def dashboard(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    account: CurrentAccount,
    period: str = PERIOD_MONTH,
) -> Response:
    """Обзор: показатели, организации, ключи."""
    since = month_start(utcnow()) if period == PERIOD_MONTH else None
    return _page(
        templates,
        request,
        "dashboard.html",
        settings,
        account=account,
        stats=await collect_account_stats(db, account.account, since=since),
        tokens=await crud.list_tokens(db, account.account.id),
        period=period,
        issued_token=request.query_params.get("token", ""),
        organization_alias=await _default_alias(db, account.account.id),
    )


async def _default_alias(db: AsyncSession, account_id: uuid.UUID) -> str:
    """Алиас организации по умолчанию — для примера интеграции."""
    result = await db.execute(
        select(Organization.alias)
        .where(Organization.account_id == account_id)
        .order_by(Organization.is_default.desc(), Organization.alias)
        .limit(1)
    )
    return result.scalar_one_or_none() or "main"


@router.get("/statistics")
async def statistics(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    account: CurrentAccount,
    period: str = PERIOD_MONTH,
) -> Response:
    """Статистика по организациям и история счетов."""
    since = month_start(utcnow()) if period == PERIOD_MONTH else None
    return _page(
        templates,
        request,
        "statistics.html",
        settings,
        account=account,
        stats=await collect_account_stats(db, account.account, since=since),
        invoices=await list_recent_invoices(
            db, account.account.id, limit=INVOICE_PAGE_SIZE
        ),
        period=period,
    )


@router.get("/balance")
async def balance(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    account: CurrentAccount,
) -> Response:
    """Баланс и выписка по операциям."""
    ledger = await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.account_id == account.account.id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(LEDGER_PAGE_SIZE)
    )
    return _page(
        templates,
        request,
        "balance.html",
        settings,
        account=account,
        ledger=list(ledger.scalars()),
        ledger_balance=await calculate_balance_from_ledger(db, account.account.id),
    )


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


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


OrganizationField = Annotated[str, Form()]


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
    """Собирает введённое — для повторной отрисовки формы."""
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


def _requisites(values: FormValues, default_fee_bps: int) -> crud.RequisitesInput:
    """Переводит значения формы в проверяемый набор реквизитов.

    :raises ValueError: если ставка или средний чек введены неверно.
    """
    from nosbp.admin.stats import parse_fee_percent

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
    """Сохраняет присланный логотип и привязывает его к организации."""
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
    if previous is not None:
        logo_cache.invalidate(previous)


@router.get("/organizations/new")
async def organization_new_form(
    request: Request,
    settings: AppSettings,
    templates: Templates,
    account: CurrentAccount,
) -> Response:
    """Форма добавления организации."""
    return _page(
        templates,
        request,
        "organization_form.html",
        settings,
        account=account,
        organization=None,
        form=_organization_values(None, settings),
    )


@router.post("/organizations/new")
async def organization_create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    account: CsrfProtected,
    logo_cache: LogoCacheDep,
    alias: OrganizationField,
    name: OrganizationField,
    personal_acc: OrganizationField,
    bank_name: OrganizationField,
    bic: OrganizationField,
    corresp_acc: OrganizationField,
    payee_inn: OrganizationField,
    kpp: OrganizationField = "",
    qr_color: OrganizationField = DEFAULT_QR_COLOR,
    fee_percent: OrganizationField = "",
    average_check: OrganizationField = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Добавляет организацию."""
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
        data = _requisites(submitted, settings.default_acquiring_fee_bps)
        organization = await crud.create_organization(db, account.account, data)
        await _attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await _reload(db, account.account)
        return _page(
            templates,
            request,
            "organization_form.html",
            settings,
            account=account,
            err=_error_text(error),
            organization=None,
            form=submitted,
        )

    log.info(
        "cabinet_organization_created",
        account=account.email,
        organization=organization.alias,
    )
    return _redirect(settings, "/", ok=f"Организация «{organization.alias}» добавлена.")


@router.get("/organizations/{organization_id}")
async def organization_edit_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    account: CurrentAccount,
    organization_id: uuid.UUID,
) -> Response:
    """Форма изменения организации."""
    organization = await _own_organization(db, account.account.id, organization_id)
    return _page(
        templates,
        request,
        "organization_form.html",
        settings,
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
    account: CsrfProtected,
    logo_cache: LogoCacheDep,
    organization_id: uuid.UUID,
    alias: OrganizationField,
    name: OrganizationField,
    personal_acc: OrganizationField,
    bank_name: OrganizationField,
    bic: OrganizationField,
    corresp_acc: OrganizationField,
    payee_inn: OrganizationField,
    kpp: OrganizationField = "",
    qr_color: OrganizationField = DEFAULT_QR_COLOR,
    fee_percent: OrganizationField = "",
    average_check: OrganizationField = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Сохраняет организацию."""
    organization = await _own_organization(db, account.account.id, organization_id)
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
        data = _requisites(submitted, settings.default_acquiring_fee_bps)
        await crud.update_organization(db, organization, data)
        await _attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await _reload(db, account.account, organization)
        return _page(
            templates,
            request,
            "organization_form.html",
            settings,
            account=account,
            err=_error_text(error),
            organization=organization,
            form=submitted,
        )

    log.info(
        "cabinet_organization_updated",
        account=account.email,
        organization=organization.alias,
    )
    return _redirect(settings, "/", ok="Организация сохранена.")


@router.post("/organizations/{organization_id}/delete")
async def organization_delete(
    db: DbSession,
    settings: AppSettings,
    account: CsrfProtected,
    organization_id: uuid.UUID,
) -> RedirectResponse:
    """Удаляет организацию вместе с её счетами."""
    organization = await _own_organization(db, account.account.id, organization_id)
    alias = organization.alias
    await crud.delete_organization(db, organization)
    await db.commit()

    log.warning(
        "cabinet_organization_deleted", account=account.email, organization=alias
    )
    return _redirect(settings, "/", ok=f"Организация «{alias}» удалена.")


# ---------------------------------------------------------------------------
# Ключи API
# ---------------------------------------------------------------------------


@router.post("/tokens")
async def token_issue(
    db: DbSession,
    settings: AppSettings,
    account: CsrfProtected,
    label: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Выпускает ключ и показывает его один раз."""
    token = await crud.issue_token(db, account.account, label=label)
    await db.commit()

    log.info("cabinet_token_issued", account=account.email)
    return _redirect(
        settings,
        "/",
        token=token,
        ok="Ключ выпущен. Он показывается один раз — скопируйте его сейчас.",
    )


@router.post("/tokens/{token_id}/revoke")
async def token_revoke(
    db: DbSession,
    settings: AppSettings,
    account: CsrfProtected,
    token_id: uuid.UUID,
) -> RedirectResponse:
    """Отзывает ключ.

    Фильтр по владельцу обязателен: без него можно было бы отозвать
    чужой ключ, подставив его идентификатор.
    """
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.id == token_id,
            ApiToken.account_id == account.account.id,
        )
    )
    api_token = result.scalar_one_or_none()
    if api_token is None:
        return _redirect(settings, "/", err="Ключ не найден.")

    if api_token.revoked_at is None:
        api_token.revoked_at = utcnow()
        await db.commit()
        log.warning(
            "cabinet_token_revoked", account=account.email, prefix=api_token.prefix
        )
    return _redirect(settings, "/", ok="Ключ отозван.")


# ---------------------------------------------------------------------------
# Пароль
# ---------------------------------------------------------------------------


@router.post("/password")
async def change_password(
    db: DbSession,
    settings: AppSettings,
    account: CsrfProtected,
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
            account.account, current=current_password, new=new_password
        )
    except NosbpError as error:
        await db.rollback()
        return _redirect(settings, "/balance", err=_error_text(error))

    log.info("cabinet_password_changed", account=account.email)
    response = _redirect(settings, "/login", ok="Пароль изменён. Войдите заново.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.cabinet_prefix or "/")
    return response
