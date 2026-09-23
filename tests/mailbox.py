"""Почтовый канал для тестов.

Складывает письма в список вместо отправки: тесты проверяют, что письмо
ушло по нужному адресу и что в нём есть рабочая ссылка, не поднимая
почтовый сервер.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SentLetter:
    """Отправленное письмо."""

    to: str
    subject: str
    text: str
    html: str

    def link_with(self, fragment: str) -> str:
        """Достаёт из письма ссылку, содержащую указанный кусок пути.

        :raises AssertionError: подходящей ссылки в письме нет.
        """
        for word in self.text.split():
            if fragment in word and word.startswith("http"):
                return word
        raise AssertionError(f"в письме нет ссылки с «{fragment}»: {self.text}")

    def token_after(self, fragment: str) -> str:
        """Возвращает токен — последний сегмент ссылки."""
        return self.link_with(fragment).rsplit("/", 1)[1]


class RecordingSender:
    """Канал доставки, который ничего не отправляет."""

    def __init__(self) -> None:
        self.sent: list[SentLetter] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def is_configured(self) -> bool:
        """Настроен: тесты должны видеть тот же путь, что и продакшен."""
        return True

    async def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        self.sent.append(SentLetter(to=to, subject=subject, text=text, html=html))

    @property
    def last(self) -> SentLetter:
        """Последнее письмо.

        :raises AssertionError: писем не было.
        """
        assert self.sent, "почта пуста: письмо не отправлено"
        return self.sent[-1]

    def clear(self) -> None:
        """Забывает отправленные письма."""
        self.sent.clear()
