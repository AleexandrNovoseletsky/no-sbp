"""Выпуск и проверка ключей API.

Токен передаётся в GET-параметре, потому что шаблонизаторы CRM умеют
вставлять только адрес картинки и не умеют ни заголовков, ни POST.

В базе хранится только SHA-256 от токена. Медленный KDF вроде argon2 здесь
не нужен и вреден: токен — это 32 случайных байта, перебрать их невозможно
в принципе, а хэшировать его придётся на каждом запросе.
"""

import hashlib
import secrets

TOKEN_BYTES = 32
PREFIX_LENGTH = 8


def generate_token() -> str:
    """Создаёт новый токен. Показывается заказчику ровно один раз."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Считает хэш токена — то, что лежит в базе."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """Первые символы токена, которые можно показывать в списке ключей."""
    return token[:PREFIX_LENGTH]
