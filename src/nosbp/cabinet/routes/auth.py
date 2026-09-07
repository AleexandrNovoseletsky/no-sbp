"""Регистрация, вход, восстановление пароля и подтверждение адреса."""

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, BackgroundTasks, Form, Request, Response
from fastapi.responses import RedirectResponse

from nosbp.cabinet import access, invites
from nosbp.cabinet.dependencies import CsrfProtected, Templates
from nosbp.cabinet.service import SESSION_COOKIE_NAME, CabinetAuthService
from nosbp.core.config import Settings
from nosbp.core.errors import (
    AccountDisabledError,
    CabinetAuthError,
    CabinetLockedError,
    EmailNotConfirmedError,
    NosbpError,
    ValidationError,
)
from nosbp.core.middleware import client_address
from nosbp.db.models import Account, InvitePurpose
from nosbp.mail.service import Mailer
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.dependencies import MailerDep
from nosbp.web.responses import SECONDS_PER_HOUR, FormValues, error_text, page, redirect

router = APIRouter()
log = structlog.get_logger()

EMPTY_REGISTER_FORM: Final[FormValues] = {"email": "", "display_name": ""}

RESET_REQUESTED_MESSAGE: Final[str] = (
    "Если этот адрес зарегистрирован, мы отправили на него письмо "
    "со ссылкой. Проверьте почту, в том числе папку «Спам»."
)
"""Ответ на запрос восстановления.

Один и тот же текст независимо от того, нашёлся адрес или нет: иначе
формой можно было бы проверять, кто пользуется сервисом.
"""

CONFIRMATION_SENT_MESSAGE: Final[str] = "Письмо со ссылкой отправлено. Проверьте почту."


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


def _deliver(
    background: BackgroundTasks, mailer: Mailer, delivery: access.Delivery | None
) -> None:
    """Ставит письмо в очередь на отправку после ответа.

    Письмо уходит именно фоновой задачей, а не в обработчике: отправка
    занимает секунды, и по времени ответа было бы видно, отправляли ли
    письмо на самом деле.
    """
    if delivery is not None:
        background.add_task(mailer.send, delivery.to, delivery.letter)


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
    mailer: MailerDep,
    background: BackgroundTasks,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    """Создаёт учётную запись и отправляет письмо с подтверждением адреса."""
    submitted: FormValues = {"email": email, "display_name": display_name}
    service = CabinetAuthService(db, settings)

    try:
        if password != password_repeat:
            raise ValidationError("Пароли не совпадают.")
        account = await service.register(
            email=email, password=password, display_name=display_name
        )
        confirmation = await access.prepare_email_confirmation(db, settings, account)
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
    _deliver(background, mailer, confirmation)

    # При обязательном подтверждении вход открывается только после
    # перехода по ссылке из письма: иначе адрес остаётся непроверенным,
    # и восстановить доступ по нему будет некуда.
    if settings.cabinet_require_email_confirmation:
        return redirect(
            settings.cabinet_prefix,
            "/login",
            ok="Аккаунт создан. Подтвердите адрес по ссылке из письма.",
        )

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
    except (
        CabinetAuthError,
        CabinetLockedError,
        AccountDisabledError,
        EmailNotConfirmedError,
    ) as error:
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
# Восстановление пароля
# ---------------------------------------------------------------------------


@router.get("/forgot")
async def forgot_form(
    request: Request, templates: Templates, settings: AppSettings
) -> Response:
    """Форма запроса ссылки для установки нового пароля."""
    return page(templates, request, "forgot.html", settings, form={"email": ""})


@router.post("/forgot")
async def forgot(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    mailer: MailerDep,
    background: BackgroundTasks,
    email: Annotated[str, Form()],
) -> Response:
    """Отправляет ссылку для установки нового пароля.

    Отвечает одинаково во всех случаях — и когда письмо ушло, и когда
    такого адреса нет, и когда лимит писем на час исчерпан.
    """
    delivery = await access.prepare_password_reset(db, settings, email=email)
    await db.commit()

    log.info(
        "cabinet_password_reset_requested",
        address=client_address(request),
        issued=delivery is not None,
    )
    _deliver(background, mailer, delivery)

    return page(
        templates,
        request,
        "forgot.html",
        settings,
        form={"email": ""},
        ok=RESET_REQUESTED_MESSAGE,
    )


# ---------------------------------------------------------------------------
# Установка пароля по ссылке
# ---------------------------------------------------------------------------


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
        account = await invites.find_account(db, token, purpose=InvitePurpose.PASSWORD)
    except NosbpError as error:
        return page(
            templates,
            request,
            "forgot.html",
            settings,
            form={"email": ""},
            err=error_text(error),
        )

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
    mailer: MailerDep,
    background: BackgroundTasks,
    token: str,
    password: Annotated[str, Form()],
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    """Устанавливает пароль, закрывает чужие сессии и выполняет вход."""
    service = CabinetAuthService(db, settings)
    try:
        if password != password_repeat:
            raise ValidationError("Пароли не совпадают.")
        # Заглянуть в аккаунт нужно до установки пароля: уведомление
        # о смене отправляется только если пароль был. Первому входу
        # по ссылке от оператора такое письмо ни к чему.
        known = await invites.find_account(db, token, purpose=InvitePurpose.PASSWORD)
        replaced = known.password_hash is not None

        account = await invites.use(db, token, password)
        # Все прежние сессии закрываются: если пароль восстанавливали
        # из-за угона, чужой открытый кабинет должен закрыться.
        await service.sessions.close_all(account.id)
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

    log.info("cabinet_password_set", account=account.email, replaced=replaced)
    if replaced:
        _deliver(
            background,
            mailer,
            access.password_changed_notice(
                settings, email=account.email, address=client_address(request)
            ),
        )

    return await _sign_in(request, service, account, settings, ok="Пароль установлен.")


# ---------------------------------------------------------------------------
# Подтверждение адреса почты
# ---------------------------------------------------------------------------


@router.get("/confirm/{token}")
async def confirm_email(
    request: Request,
    db: DbSession,
    templates: Templates,
    settings: AppSettings,
    token: str,
) -> Response:
    """Подтверждает адрес почты по ссылке из письма."""
    try:
        account = await invites.confirm(db, token)
        await db.commit()
    except NosbpError as error:
        await db.rollback()
        return page(templates, request, "login.html", settings, err=error_text(error))

    log.info("cabinet_email_confirmed", account=account.email)
    return redirect(
        settings.cabinet_prefix, "/login", ok="Адрес подтверждён. Можно входить."
    )


@router.post("/confirm")
async def resend_confirmation(
    db: DbSession,
    settings: AppSettings,
    mailer: MailerDep,
    background: BackgroundTasks,
    viewer: CsrfProtected,
) -> RedirectResponse:
    """Отправляет письмо с подтверждением адреса ещё раз."""
    if viewer.account.email_confirmed_at is not None:
        return redirect(settings.cabinet_prefix, "/", ok="Адрес уже подтверждён.")

    delivery = await access.prepare_email_confirmation(db, settings, viewer.account)
    await db.commit()
    _deliver(background, mailer, delivery)

    return redirect(settings.cabinet_prefix, "/", ok=CONFIRMATION_SENT_MESSAGE)
