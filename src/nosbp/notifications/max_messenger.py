"""Доставка оповещений через Bot API мессенджера MAX.

Токен выдаёт бот @MasterBot, идентификатор чата приходит в обновлениях
после первого сообщения боту. Формат запроса описан в документации
разработчика MAX; при изменении API правится только этот модуль.
"""

from typing import Final

import httpx

API_BASE_URL: Final[str] = "https://botapi.max.ru"
MESSAGES_PATH: Final[str] = "/messages"


class MaxChannel:
    """Отправка сообщений в чат MAX от имени бота."""

    def __init__(self, *, token: str, chat_id: str, timeout: float) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "max"

    async def send(self, text: str) -> None:
        """Отправляет сообщение в настроенный чат."""
        params = {"access_token": self._token, "chat_id": self._chat_id}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{API_BASE_URL}{MESSAGES_PATH}",
                params=params,
                json={"text": text},
            )
            response.raise_for_status()
