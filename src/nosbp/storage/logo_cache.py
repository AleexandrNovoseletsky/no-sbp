"""Кэш логотипов в памяти процесса.

Логотип организации меняется раз в год, а читается на каждой генерации QR.
Ходить за ним в S3 каждый раз — значит добавлять десятки миллисекунд
сетевого похода к операции, которая сама занимает единицы миллисекунд.
"""

import time

from nosbp.storage.base import Storage

DEFAULT_TTL_SECONDS = 600


class LogoCache:
    """Кэш «ключ хранилища → байты логотипа» с ограниченным временем жизни."""

    def __init__(self, storage: Storage, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._storage = storage
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, bytes | None]] = {}

    async def get(self, key: str | None) -> bytes | None:
        """Отдаёт логотип по ключу хранилища.

        Отсутствие логотипа тоже кэшируется — иначе организации без
        логотипа порождали бы поход в S3 на каждом запросе.
        """
        if key is None:
            return None

        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]

        data = await self._storage.get(key)
        self._entries[key] = (now + self._ttl, data)
        return data

    def invalidate(self, key: str) -> None:
        """Сбрасывает кэш для одного ключа — после замены логотипа."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Полностью очищает кэш."""
        self._entries.clear()
