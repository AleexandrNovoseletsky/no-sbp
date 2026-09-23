"""Вход в панель управления."""

from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Form, Request, Response
from fastapi.responses import RedirectResponse

from nosbp.admin.dependencies import CsrfProtected, NotifierDep, Templates
from nosbp.admin.security import SESSION_COOKIE_NAME
from nosbp.admin.service import AdminAuthService
from nosbp.core.config import Settings
from nosbp.core.errors import AdminAuthError, AdminLockedError
from nosbp.core.middleware import client_address
from nosbp.db.base import utcnow
from nosbp.notifications.service import (
    Notifier,
    admin_login_failed_message,
    admin_login_message,
)
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import SECONDS_PER_HOUR, page, redirect

router = APIRouter()
log = structlog.get_logger()


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
    notifier: NotifierDep,
    background: BackgroundTasks,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    totp_code: Annotated[str, Form()] = "",
) -> Response:
    """Проверяет учётные данные и открывает сессию."""
    service = AdminAuthService(db, settings)
    address = client_address(request)
    user_agent = request.headers.get("user-agent", "")

    try:
        admin = await service.authenticate(
            email=email, password=password, totp_code=totp_code
        )
    except (AdminAuthError, AdminLockedError) as error:
        log.warning("admin_login_failed", email=email, address=address)
        _notify(
            background,
            notifier=notifier,
            settings=settings,
            enabled=settings.notify_admin_login_failed,
            text=admin_login_failed_message(
                email=email,
                address=address,
                user_agent=user_agent,
                moment=utcnow(),
                base_url=settings.public_base_url,
                locked=isinstance(error, AdminLockedError),
            ),
        )
        return page(
            templates, request, "login.html", settings, err=error.message, email=email
        )

    issued = await service.open_session(
        admin, ip_address=address, user_agent=user_agent
    )
    log.info("admin_login", admin=admin.email, address=address)
    _notify(
        background,
        notifier=notifier,
        settings=settings,
        enabled=settings.notify_admin_login,
        text=admin_login_message(
            email=admin.email,
            address=address,
            user_agent=user_agent,
            moment=utcnow(),
            base_url=settings.public_base_url,
        ),
    )

    response = redirect(settings.admin_prefix, "/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.token,
        max_age=settings.admin_session_ttl_hours * SECONDS_PER_HOUR,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path=settings.admin_prefix or "/",
    )
    return response


def _notify(
    background: BackgroundTasks,
    *,
    notifier: Notifier,
    settings: Settings,
    enabled: bool,
    text: str,
) -> None:
    """Планирует оповещение после того, как ответ отдан клиенту.

    Сеть мессенджера не должна задерживать вход.
    """
    if enabled and notifier.is_configured:
        background.add_task(notifier.send, text)


@router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
) -> RedirectResponse:
    """Закрывает сессию.

    Тоже под защитой токена: без неё чужая страница могла бы выкидывать
    администратора из панели на каждом заходе.
    """
    await AdminAuthService(db, settings).close_session(
        request.cookies.get(SESSION_COOKIE_NAME)
    )
    response = redirect(settings.admin_prefix, "/login", ok="Вы вышли из панели.")
    response.delete_cookie(SESSION_COOKIE_NAME, path=settings.admin_prefix or "/")
    return response
