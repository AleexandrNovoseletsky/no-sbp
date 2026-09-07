"""Доставка писем через внешний SMTP-сервер."""

from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Final

import aiosmtplib
import structlog

from nosbp.core.constants import SmtpSecurity

log = structlog.get_logger()

HTML_SUBTYPE: Final[str] = "html"

AUTO_SUBMITTED: Final[str] = "auto-generated"
"""Заголовок по RFC 3834.

Помечает письмо как отправленное автоматом: почтовые роботы не станут
отвечать на него автоответом об отпуске, а мы не получим петлю.
"""


class SmtpSender:
    """Отправка писем через SMTP.

    Соединение открывается на каждое письмо и сразу закрывается. Держать
    его постоянно нет смысла: писем единицы в день, а долгоживущее
    соединение пришлось бы переоткрывать после каждого разрыва.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        security: SmtpSecurity,
        sender_address: str,
        sender_name: str,
        timeout: float,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._security = security
        self._sender_address = sender_address
        self._sender_name = sender_name
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "smtp"

    @property
    def is_configured(self) -> bool:
        """Готов ли канал: без сервера и адреса отправителя писать некуда."""
        return bool(self._host and self._sender_address)

    async def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        """Отправляет письмо. Ошибки доставки пробрасываются наверх."""
        message = self._build(to=to, subject=subject, text=text, html=html)
        await aiosmtplib.send(
            message,
            hostname=self._host,
            port=self._port,
            username=self._username or None,
            password=self._password or None,
            use_tls=self._security is SmtpSecurity.SSL,
            start_tls=self._security is SmtpSecurity.STARTTLS,
            timeout=self._timeout,
        )

    def _build(self, *, to: str, subject: str, text: str, html: str) -> EmailMessage:
        """Собирает письмо из двух частей: текстовой и оформленной.

        Порядок частей важен: почтовый клиент показывает последнюю, которую
        умеет отобразить, поэтому HTML добавляется после текста.
        """
        message = EmailMessage()
        message["From"] = formataddr((self._sender_name, self._sender_address))
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        # Идентификатор письма собирается по домену отправителя: без него
        # библиотека подставит имя хоста машины, и часть фильтров сочтёт
        # такое письмо подозрительным.
        message["Message-ID"] = make_msgid(domain=self._sender_domain)
        message["Auto-Submitted"] = AUTO_SUBMITTED

        message.set_content(text)
        message.add_alternative(html, subtype=HTML_SUBTYPE)
        return message

    @property
    def _sender_domain(self) -> str:
        """Домен адреса отправителя."""
        return self._sender_address.rpartition("@")[2]


class NullSender:
    """Заглушка на время, пока почта не настроена.

    Письмо не отправляется, а записывается в журнал вместе со ссылкой.
    Так локальная разработка и первый запуск на сервере не упираются
    в почтовый сервер: ссылку для установки пароля видно в логах.
    """

    @property
    def name(self) -> str:
        return "null"

    @property
    def is_configured(self) -> bool:
        return False

    async def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        """Записывает письмо в журнал вместо отправки."""
        log.warning("mail_not_configured", to=to, subject=subject, body=text)
