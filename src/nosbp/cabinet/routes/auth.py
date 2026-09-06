"""Регистрация, вход и установка пароля."""

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse

from nosbp.cabinet import invites
from nosbp.cabinet.dependencies import CsrfProtected, Templates
from nosbp.cabinet.service import SESSION_COOKIE_NAME, CabinetAuthService
from nosbp.core.config import Settings
from nosbp.core.errors import (
    AccountDisabledError,
    CabinetAuthError,
    CabinetLockedError,
    NosbpError,
    ValidationError,
)
from nosbp.core.middleware import client_address
from nosbp.db.models import Account
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import SECONDS_PER_HOUR, FormValues, error_text, page, redirect

router = APIRouter()
log = structlog.get_logger()

EMPTY_REGISTER_FORM: Final[FormValues] = {"email": "", "display_name": ""}


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


async def _sign_in(
    request: Request,
    service: CabinetAuthService,
    account: Account,
    settings: Settings,
    *,
    ok: str = "",
) -> RedirectResponse:
    """Открывает сессию и отправляет заказчика на обзор."""
    issued = await service.open_session(
        account,
        ip_address=client_address(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    response = redirect(settings.cabinet_prefix, "/", ok=ok)
    _set_session_cookie(response, issued.token, settings)
    return response


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


@router.get("/register")
async def register_form(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Страница регистрации."""
    return page(templates, request, "register.html", settings, form=EMPTY_REGISTER_FORM)


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
        return page(
            templates,
            request,
            "register.html",
            settings,
            err=error_text(error),
            form=submitted,
        )

    log.info("cabinet_registered", account=account.email)
    return await _sign_in(
        request, service, account, settings, ok="Аккаунт создан. Добавьте организацию."
    )


# ---------------------------------------------------------------------------
# Вход и выход
# ---------------------------------------------------------------------------


@router.get("/login")
async def login_form(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Страница входа."""
    return page(templates, request, "login.html", settings)


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
        return page(
            templates, request, "login.html", settings, err=error.message, email=email
        )

    log.info("cabinet_login", account=account.email)
    return await _sign_in(request, service, account, settings)


@router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
) -> RedirectResponse:
    """Закрывает сессию."""
    await CabinetAuthService(db, settings).close_session(
        request.cookies.get(SESSION_COOKIE_NAME)
    )
    response = redirect(settings.cabinet_prefix, "/login", ok="Вы вышли из кабинета.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.cabinet_prefix or "/")
    return response


# ---------------------------------------------------------------------------
# Установка и восстановление пароля
# ---------------------------------------------------------------------------


@router.get("/forgot")
async def forgot_password(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Как получить доступ, если пароль забыт или не задавался.

    Самостоятельного восстановления пока нет: письма сервис не отправляет,
    поэтому ссылку выдаёт поддержка.
    """
    return page(templates, request, "forgot.html", settings)


@router.get("/password/{token}")
async def set_password_form(
    request: Request,
    db: DbSession,
    templates: Templates,
    settings: AppSettings,
    token: str,
) -> Response:
    """Страница установки пароля по одноразовой ссылке."""
    try:
        account = await invites.find_account(db, token)
    except NosbpError as error:
        return page(templates, request, "forgot.html", settings, err=error_text(error))

    return page(
        templates,
        request,
        "set_password.html",
        settings,
        token=token,
        invited_email=account.email,
    )


@router.post("/password/{token}")
async def set_password(
    request: Request,
    db: DbSession,
    templates: Templates,
    settings: AppSettings,
    token: str,
    password: Annotated[str, Form()],
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    """Устанавливает пароль и выполняет вход."""
    service = CabinetAuthService(db, settings)
    try:
        if password != password_repeat:
            raise ValidationError("Пароли не совпадают.")
        account = await invites.use(db, token, password)
        await db.commit()
    except NosbpError as error:
        await db.rollback()
        return page(
            templates,
            request,
            "set_password.html",
            settings,
            err=error_text(error),
            token=token,
            invited_email="",
        )

    log.info("cabinet_password_set", account=account.email)
    return await _sign_in(request, service, account, settings, ok="Пароль установлен.")
