"""Зависимости панели управления: текущая сессия и защита от CSRF."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin.security import (
    SESSION_COOKIE_NAME,
)
from nosbp.admin.service import AdminAuthService
from nosbp.core.config import Settings, get_settings
from nosbp.core.errors import AdminAuthError
from nosbp.core.security import (
    CSRF_FIELD_NAME,
    csrf_tokens_match,
)
from nosbp.db.models import AdminSession, AdminUser
from nosbp.db.session import get_db
from nosbp.payments.dependencies import AppSettings, DbSession, LogoCacheDep
from nosbp.web.dependencies import MailerDep, NotifierDep
from nosbp.web.responses import template_globals

TEMPLATES_DIRECTORY = "templates"
SHARED_TEMPLATES = Path(__file__).parent.parent / "web" / "templates"
"""Каталог с шаблонами, общими для панели и кабинета."""


def build_templates(settings: Settings) -> Jinja2Templates:
    """Собирает движок шаблонов с общими для всех страниц значениями.

    Форматирование денег и процентов подключается сюда один раз, а не
    передаётся в контексте каждой страницы: иначе о нём приходится помнить
    при добавлении любого нового шаблона.
    """
    templates = Jinja2Templates(
        directory=[
            Path(__file__).parent / TEMPLATES_DIRECTORY,
            SHARED_TEMPLATES,
        ]
    )
    templates.env.globals.update(
        template_globals("admin_prefix", settings.admin_prefix)
    )
    return templates


def get_templates(request: Request) -> Jinja2Templates:
    """Отдаёт движок шаблонов, созданный при старте приложения."""
    templates: Jinja2Templates = request.app.state.admin_templates
    return templates


class AdminContext:
    """Всё, что нужно странице панели: администратор и его сессия.

    Почта и токен CSRF снимаются копией сразу: после отката транзакции
    объекты ORM устаревают, и обращение к ним из шаблона привело бы
    к запросу в базу во время отрисовки.
    """

    def __init__(self, admin: AdminUser, session: AdminSession) -> None:
        self.admin = admin
        self.session = session
        self.email = admin.email
        self.display_name = admin.email
        self.csrf_token = session.csrf_token


async def current_admin(
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> AdminContext:
    """Проверяет куку сессии и отдаёт текущего администратора.

    :raises AdminAuthError: сессии нет, она истекла или отозвана. Обработчик
        в маршрутах превращает эту ошибку в переход на страницу входа.
    """
    service = AdminAuthService(db, settings)
    session = await service.load_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise AdminAuthError("Требуется вход.")

    admin = await db.get(AdminUser, session.owner_id)
    if admin is None or not admin.is_active:
        raise AdminAuthError("Требуется вход.")

    return AdminContext(admin=admin, session=session)


CurrentAdmin = Annotated[AdminContext, Depends(current_admin)]


async def verify_csrf(
    admin: CurrentAdmin,
    csrf_token: Annotated[str | None, Form(alias=CSRF_FIELD_NAME)] = None,
) -> AdminContext:
    """Проверяет токен формы.

    Без этой проверки чужая страница могла бы отправить форму от имени
    открытой сессии администратора — и, например, обнулить чей-то баланс.

    :raises AdminAuthError: если токен не совпал.
    """
    if not csrf_tokens_match(admin.csrf_token, csrf_token):
        raise AdminAuthError("Форма устарела. Обновите страницу и повторите.")
    return admin


CsrfProtected = Annotated[AdminContext, Depends(verify_csrf)]

__all__ = [
    "AdminContext",
    "AdminDb",
    "AdminSettings",
    "CsrfProtected",
    "CurrentAdmin",
    "LogoCacheDep",
    "MailerDep",
    "NotifierDep",
    "Templates",
    "build_templates",
]

AdminDb = Annotated[AsyncSession, Depends(get_db)]
AdminSettings = Annotated[Settings, Depends(get_settings)]
Templates = Annotated[Jinja2Templates, Depends(get_templates)]
