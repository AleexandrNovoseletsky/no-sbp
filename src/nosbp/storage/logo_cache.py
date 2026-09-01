"""Кэш логотипов в памяти процесса.

Логотип организации меняется раз в год, а читается на каждой генерации QR.
Ходить за ним в объектное хранилище каждый раз — значит добавлять десятки
миллисекунд сетевого похода к операции, которая сама занимает единицы
миллисекунд.
"""

import time
from collections import OrderedDict

from nosbp.storage.base import Storage


class LogoCache:
    """Кэш «ключ хранилища → байты логотипа» с ограниченным сроком и размером.

    Размер ограничен намеренно: без предела кэш растёт вместе с числом
    организаций у всех заказчиков и однажды съедает всю память процесса.
    Вытесняется то, к чему дольше всего не обращались.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        ttl_seconds: int,
        max_entries: int,
    ) -> None:
        self._storage = storage
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, bytes | None]] = OrderedDict()

    async def get(self, key: str | None) -> bytes | None:
        """Отдаёт логотип по ключу хранилища.

        Отсутствие логотипа тоже кэшируется: иначе организации без
        логотипа порождали бы поход в хранилище на каждом запросе.

        :param key: ключ файла или None, если логотип не задан.
        """
        if key is None:
            return None

        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None:
            expires_at, data = cached
            if now < expires_at:
                # Обращение делает запись «свежей» для вытеснения.
                self._entries.move_to_end(key)
                return data
            del self._entries[key]

        data = await self._storage.get(key)
        self._put(key, data, now)
        return data

    def invalidate(self, key: str) -> None:
        """Сбрасывает кэш для одного ключа — после замены логотипа."""
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Полностью очищает кэш."""
        self._entries.clear()

    def _put(self, key: str, data: bytes | None, now: float) -> None:
        """Кладёт запись, вытесняя самую давнюю при переполнении."""
        self._entries[key] = (now + self._ttl_seconds, data)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)
