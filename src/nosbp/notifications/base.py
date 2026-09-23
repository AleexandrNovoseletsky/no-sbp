"""Абстракция канала оповещений."""

from typing import Protocol


class NotificationChannel(Protocol):
    """Канал доставки коротких текстовых сообщений оператору сервиса."""

    @property
    def name(self) -> str:
        """Имя канала для журнала."""
        ...

    async def send(self, text: str) -> None:
        """Отправляет сообщение.

        :raises Exception: любая ошибка доставки. Вызывающий код обязан
            её обработать: оповещения не должны влиять на основную
            операцию.
        """
        ...
