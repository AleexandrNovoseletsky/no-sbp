"""Отправка писем заказчику.

Письмо никогда не влияет на исход операции, которая его породила:
недоступный почтовый сервер не должен мешать сменить пароль. Поэтому
:meth:`Mailer.send` не пробрасывает ошибки наружу, а возвращает признак
доставки и пишет в журнал.
"""

import structlog

from nosbp.core.config import Settings
from nosbp.core.constants import SERVICE_NAME
from nosbp.mail.base import Letter, MailSender
from nosbp.mail.render import render_html, render_text
from nosbp.mail.smtp import NullSender, SmtpSender

log = structlog.get_logger()


def build_sender(settings: Settings) -> MailSender:
    """Собирает канал доставки по настройкам.

    Пока почта не настроена, письма пишутся в журнал: сервис остаётся
    работоспособным, а ссылку для установки пароля можно достать из логов.
    """
    if not settings.smtp_host or not settings.mail_from:
        return NullSender()

    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_user,
        password=settings.smtp_password,
        security=settings.smtp_security,
        sender_address=settings.mail_from,
        sender_name=settings.mail_from_name,
        timeout=settings.smtp_timeout_seconds,
    )


class Mailer:
    """Отправляет письма и переживает отказ почтового сервера."""

    def __init__(self, sender: MailSender, *, base_url: str) -> None:
        self._sender = sender
        self._base_url = base_url

    @property
    def is_configured(self) -> bool:
        """Настроена ли отправка по-настоящему."""
        return self._sender.is_configured

    async def send(self, to: str, letter: Letter) -> bool:
        """Отправляет письмо.

        :return: True, если письмо ушло. False — если доставка не удалась
            или почта не настроена; вызывающий код решает, показывать ли
            это человеку.
        """
        text = render_text(letter, service_name=SERVICE_NAME, base_url=self._base_url)
        html = render_html(letter, service_name=SERVICE_NAME, base_url=self._base_url)

        try:
            await self._sender.send(to=to, subject=letter.subject, text=text, html=html)
        except Exception as error:
            # Причина в журнале, но наружу не уходит: по сообщению об
            # ошибке доставки можно было бы установить, существует ли
            # адрес в чужой почтовой системе.
            log.error(
                "mail_failed",
                channel=self._sender.name,
                subject=letter.subject,
                error=str(error),
            )
            return False

        if not self._sender.is_configured:
            return False

        log.info("mail_sent", subject=letter.subject)
        return True


def build_mailer(settings: Settings) -> Mailer:
    """Собирает отправку писем для приложения."""
    return Mailer(build_sender(settings), base_url=settings.public_base_url)
