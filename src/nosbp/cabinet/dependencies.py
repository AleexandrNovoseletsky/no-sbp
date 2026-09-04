"""Зависимости личного кабинета."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form, Request
from fastapi.templating import Jinja2Templates

from nosbp.admin.stats import format_fee_percent
from nosbp.cabinet.service import SESSION_COOKIE_NAME, CabinetAuthService
from nosbp.core.config import Settings
from nosbp.core.errors import CabinetAuthError
from nosbp.core.money import format_roubles, roubles_input
from nosbp.core.security import CSRF_FIELD_NAME, csrf_tokens_match
from nosbp.db.models import Account, AccountSession
from nosbp.payments.dependencies import AppSettings, DbSession

TEMPLATES_DIRECTORY = "templates"


def build_templates(settings: Settings) -> Jinja2Templates:
    """Собирает движок шаблонов кабинета."""
    templates = Jinja2Templates(directory=Path(__file__).parent / TEMPLATES_DIRECTORY)
    templates.env.globals.update(
        cabinet_prefix=settings.cabinet_prefix,
        csrf_field=CSRF_FIELD_NAME,
        format_roubles=format_roubles,
        format_fee_percent=format_fee_percent,
        roubles_input=roubles_input,
    )
    return templates


def get_templates(request: Request) -> Jinja2Templates:
    """Отдаёт движок шаблонов, созданный при старте приложения."""
    templates: Jinja2Templates = request.app.state.cabinet_templates
    return templates


class CabinetContext:
    """Заказчик и его сессия.

    Название компании и токен формы снимаются копией сразу: после отката
    транзакции объекты ORM устаревают, и обращение к ним из шаблона
    привело бы к запросу в базу во время отрисовки.
    """

    def __init__(self, account: Account, session: AccountSession) -> None:
        self.account = account
        self.session = session
        self.email = account.email
        self.display_name = account.display_name
        self.csrf_token = session.csrf_token


async def current_account(
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> CabinetContext:
    """Проверяет куку сессии и отдаёт текущего заказчика.

    :raises CabinetAuthError: сессии нет, она истекла или учётная запись
        отключена. Обработчик в приложении превращает эту ошибку
        в переход на страницу входа.
    """
    service = CabinetAuthService(db, settings)
    session = await service.load_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise CabinetAuthError("Требуется вход.")

    account = await db.get(Account, session.owner_id)
    if account is None or not account.is_active:
        raise CabinetAuthError("Требуется вход.")

    return CabinetContext(account=account, session=session)


CurrentAccount = Annotated[CabinetContext, Depends(current_account)]


async def verify_csrf(
    account: CurrentAccount,
    csrf_token: Annotated[str | None, Form(alias=CSRF_FIELD_NAME)] = None,
) -> CabinetContext:
    """Проверяет токен формы.

    :raises CabinetAuthError: если токен не совпал.
    """
    if not csrf_tokens_match(account.csrf_token, csrf_token):
        raise CabinetAuthError("Форма устарела. Обновите страницу и повторите.")
    return account


CsrfProtected = Annotated[CabinetContext, Depends(verify_csrf)]
Templates = Annotated[Jinja2Templates, Depends(get_templates)]
