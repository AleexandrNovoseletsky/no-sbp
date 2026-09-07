"""Зависимости, общие для панели управления и личного кабинета.

Ресурсы, которые живут в состоянии приложения и создаются один раз при
старте: рассылка оповещений оператору и отправка писем заказчику.
"""

from typing import Annotated

from fastapi import Depends, Request

from nosbp.mail.service import Mailer
from nosbp.notifications.service import Notifier


def get_notifier(request: Request) -> Notifier:
    """Отдаёт рассылку оповещений, созданную при старте приложения."""
    notifier: Notifier = request.app.state.notifier
    return notifier


def get_mailer(request: Request) -> Mailer:
    """Отдаёт отправку писем, созданную при старте приложения."""
    mailer: Mailer = request.app.state.mailer
    return mailer


NotifierDep = Annotated[Notifier, Depends(get_notifier)]
MailerDep = Annotated[Mailer, Depends(get_mailer)]
