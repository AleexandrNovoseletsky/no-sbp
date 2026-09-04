"""Общие примитивы аутентификации.

Используются и панелью управления, и личным кабинетом заказчика: правила
хранения паролей, выдачи сессионных токенов и защиты форм одинаковы для
обоих интерфейсов.
"""

import hashlib
import hmac
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from nosbp.core.constants import ADMIN_SESSION_TOKEN_BYTES, CSRF_TOKEN_BYTES

CSRF_FIELD_NAME: Final[str] = "csrf_token"

MIN_PASSWORD_LENGTH: Final = 12
"""Минимальная длина пароля."""

# Параметры argon2id зафиксированы явно: обновление библиотеки не должно
# незаметно изменять стойкость хэширования.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
)


def hash_password(password: str) -> str:
    """Считает хэш пароля."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Проверяет пароль, не возбуждая исключений.

    UnicodeError обрабатывается наравне с ошибками argon2: библиотека
    кодирует строку хэша в ASCII, и некорректная запись в базе иначе
    привела бы к ошибке вместо отказа в аутентификации.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
        UnicodeError,
    ):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """Пора ли пересчитать хэш из-за смены параметров argon2."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, UnicodeError):
        return True


def validate_password_strength(password: str) -> str | None:
    """Проверяет минимальные требования к паролю.

    :return: текст претензии или None, если пароль годится.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
    if password.isdigit() or password.isalpha():
        return "Пароль должен содержать и буквы, и цифры."
    return None


def generate_session_token() -> str:
    """Создаёт токен сессии. Помещается в куку и больше нигде не хранится."""
    return secrets.token_urlsafe(ADMIN_SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Считает хэш токена сессии — то, что лежит в базе."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token() -> str:
    """Создаёт токен для защиты форм."""
    return secrets.token_hex(CSRF_TOKEN_BYTES)


def csrf_tokens_match(expected: str, received: str | None) -> bool:
    """Сравнивает токены CSRF за постоянное время.

    Обычное сравнение строк выходит из цикла на первом различии, и по
    времени ответа токен можно подобрать посимвольно.
    """
    if not received:
        return False
    return hmac.compare_digest(expected, received)
