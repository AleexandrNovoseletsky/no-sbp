"""Тесты кэша логотипов: срок жизни и вытеснение."""

import pytest

from nosbp.storage.logo_cache import LogoCache
from nosbp.storage.memory import MemoryStorage


class CountingStorage(MemoryStorage):
    """Хранилище, считающее обращения — чтобы проверить, что кэш работает."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def get(self, key: str) -> bytes | None:
        self.reads += 1
        return await super().get(key)


@pytest.fixture
def storage() -> CountingStorage:
    return CountingStorage()


async def test_second_read_comes_from_cache(storage):
    await storage.put("logo", b"data", content_type="image/png")
    cache = LogoCache(storage, ttl_seconds=600, max_entries=10)

    assert await cache.get("logo") == b"data"
    assert await cache.get("logo") == b"data"
    assert storage.reads == 1


async def test_missing_logo_is_cached_too(storage):
    """Организации без логотипа не должны ходить в хранилище каждый раз."""
    cache = LogoCache(storage, ttl_seconds=600, max_entries=10)

    assert await cache.get("absent") is None
    assert await cache.get("absent") is None
    assert storage.reads == 1


async def test_none_key_never_touches_storage(storage):
    cache = LogoCache(storage, ttl_seconds=600, max_entries=10)
    assert await cache.get(None) is None
    assert storage.reads == 0


async def test_expired_entry_is_reread(storage):
    await storage.put("logo", b"data", content_type="image/png")
    cache = LogoCache(storage, ttl_seconds=0, max_entries=10)

    await cache.get("logo")
    await cache.get("logo")
    assert storage.reads == 2


async def test_invalidate_forces_reread(storage):
    await storage.put("logo", b"old", content_type="image/png")
    cache = LogoCache(storage, ttl_seconds=600, max_entries=10)
    await cache.get("logo")

    await storage.put("logo", b"new", content_type="image/png")
    cache.invalidate("logo")

    assert await cache.get("logo") == b"new"


async def test_cache_size_is_bounded(storage):
    """Без предела кэш растёт вместе с числом организаций и съедает память."""
    max_entries = 3
    cache = LogoCache(storage, ttl_seconds=600, max_entries=max_entries)

    for index in range(10):
        key = f"logo-{index}"
        await storage.put(key, b"x", content_type="image/png")
        await cache.get(key)

    assert len(cache) == max_entries


async def test_recently_used_entry_survives_eviction(storage):
    """Вытесняется то, к чему дольше всего не обращались."""
    for key in ("a", "b", "c"):
        await storage.put(key, b"x", content_type="image/png")

    cache = LogoCache(storage, ttl_seconds=600, max_entries=2)
    await cache.get("a")
    await cache.get("b")
    await cache.get("a")  # «a» снова становится свежей
    await cache.get("c")  # вытеснить должно «b»

    storage.reads = 0
    await cache.get("a")
    assert storage.reads == 0, "«a» должна была остаться в кэше"
