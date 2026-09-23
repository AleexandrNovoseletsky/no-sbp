"""Доставка оповещений через Telegram Bot API."""

from typing import Final

import httpx

API_BASE_URL: Final[str] = "https://api.telegram.org"
PARSE_MODE: Final[str] = "HTML"


class TelegramChannel:
    """Отправка сообщений в чат Telegram от имени бота.

    Токен выдаёт @BotFather, идентификатор чата можно узнать у @userinfobot
    или из ответа метода getUpdates после первого сообщения боту.
    """

    def __init__(self, *, token: str, chat_id: str, timeout: float) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "telegram"

    async def send(self, text: str) -> None:
        """Отправляет сообщение в настроенный чат."""
        url = f"{API_BASE_URL}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": PARSE_MODE,
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
