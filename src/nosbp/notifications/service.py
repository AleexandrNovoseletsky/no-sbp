"""Отправка оповещений о событиях безопасности.

Оповещения носят информационный характер и никогда не влияют на исход
операции, которая их вызвала: ошибка доставки только записывается
в журнал. Отправка выполняется после того, как ответ уже отдан клиенту.
"""

import datetime
import html
from collections.abc import Sequence
from typing import Final

import structlog

from nosbp.core.config import Settings
from nosbp.notifications.base import NotificationChannel
from nosbp.notifications.max_messenger import MaxChannel
from nosbp.notifications.telegram import TelegramChannel

log = structlog.get_logger()

DATETIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M:%S"
USER_AGENT_LIMIT: Final = 120
"""Строка браузера обрезается: в сообщении важен тип клиента, не подробности."""


def build_channels(settings: Settings) -> tuple[NotificationChannel, ...]:
    """Собирает настроенные каналы оповещений.

    Канал включается, только если заданы и токен, и идентификатор чата.
    """
    channels: list[NotificationChannel] = []

    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(
            TelegramChannel(
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                timeout=settings.notification_timeout_seconds,
            )
        )

    if settings.max_bot_token and settings.max_chat_id:
        channels.append(
            MaxChannel(
                token=settings.max_bot_token,
                chat_id=settings.max_chat_id,
                timeout=settings.notification_timeout_seconds,
            )
        )

    return tuple(channels)


class Notifier:
    """Рассылает сообщения по всем настроенным каналам."""

    def __init__(self, channels: Sequence[NotificationChannel]) -> None:
        self._channels = tuple(channels)

    @property
    def is_configured(self) -> bool:
        """Настроен ли хотя бы один канал."""
        return bool(self._channels)

    async def send(self, text: str) -> None:
        """Отправляет сообщение во все каналы.

        Отказ одного канала не мешает остальным и не пробрасывается наружу.
        """
        for channel in self._channels:
            try:
                await channel.send(text)
            except Exception as error:
                # Оповещение — вспомогательная функция. Любая её ошибка
                # не должна влиять на операцию, которая её вызвала.
                log.warning(
                    "notification_failed",
                    channel=channel.name,
                    error=str(error),
                )


def admin_login_message(
    *,
    email: str,
    address: str,
    user_agent: str,
    moment: datetime.datetime,
    base_url: str,
) -> str:
    """Формирует сообщение об успешном входе в панель управления."""
    return _message(
        title="Вход в панель управления",
        email=email,
        address=address,
        user_agent=user_agent,
        moment=moment,
        base_url=base_url,
        footer="Если это были не вы — смените пароль командой «nosbp admin password».",
    )


def admin_login_failed_message(
    *,
    email: str,
    address: str,
    user_agent: str,
    moment: datetime.datetime,
    base_url: str,
    locked: bool,
) -> str:
    """Формирует сообщение о неудачной попытке входа."""
    title = (
        "Вход заблокирован после неудачных попыток"
        if locked
        else "Неудачная попытка входа в панель"
    )
    footer = (
        "Блокировка снимается командой «nosbp admin unlock»."
        if locked
        else "Если попыток много — проверьте список разрешённых сетей."
    )
    return _message(
        title=title,
        email=email,
        address=address,
        user_agent=user_agent,
        moment=moment,
        base_url=base_url,
        footer=footer,
    )


def _message(
    *,
    title: str,
    email: str,
    address: str,
    user_agent: str,
    moment: datetime.datetime,
    base_url: str,
    footer: str,
) -> str:
    """Собирает текст сообщения.

    Значения экранируются: они приходят из запроса, а сообщение
    отправляется с разметкой HTML.
    """
    lines = (
        f"<b>{html.escape(title)}</b>",
        "",
        f"Сервис: {html.escape(base_url)}",
        f"Учётная запись: {html.escape(email)}",
        f"Адрес: {html.escape(address or 'неизвестен')}",
        f"Клиент: {html.escape(user_agent[:USER_AGENT_LIMIT] or 'не указан')}",
        f"Время: {moment.strftime(DATETIME_FORMAT)} UTC",
        "",
        html.escape(footer),
    )
    return "\n".join(lines)
