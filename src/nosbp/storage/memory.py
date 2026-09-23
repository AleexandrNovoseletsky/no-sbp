"""Хранилище в оперативной памяти — для тестов и локального запуска."""

from nosbp.storage.base import Storage


class MemoryStorage(Storage):
    """Реализация :class:`Storage` поверх обычного словаря."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self._files[key] = data

    async def get(self, key: str) -> bytes | None:
        return self._files.get(key)

    async def delete(self, key: str) -> None:
        self._files.pop(key, None)

    def __len__(self) -> int:
        return len(self._files)
