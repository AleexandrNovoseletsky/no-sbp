"""Зависимости страницы оплаты."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from nosbp.core.config import Settings
from nosbp.core.money import format_roubles

TEMPLATES_DIRECTORY = "templates"


def build_templates(settings: Settings) -> Jinja2Templates:
    """Собирает движок шаблонов страницы оплаты.

    Отдельный от панели и кабинета: у публичной страницы своя вёрстка
    и своя навигация, а общего с внутренними интерфейсами у неё нет
    ничего, кроме форматирования сумм.
    """
    templates = Jinja2Templates(directory=Path(__file__).parent / TEMPLATES_DIRECTORY)
    templates.env.globals.update(
        {
            "pay_prefix": settings.payment_prefix,
            "format_roubles": format_roubles,
        }
    )
    return templates


def get_templates(request: Request) -> Jinja2Templates:
    """Отдаёт движок шаблонов, созданный при старте приложения."""
    templates: Jinja2Templates = request.app.state.pay_templates
    return templates


PayTemplates = Annotated[Jinja2Templates, Depends(get_templates)]
