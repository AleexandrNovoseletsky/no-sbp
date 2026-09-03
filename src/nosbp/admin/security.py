"""Примитивы безопасности панели управления.

Панель управляет балансами заказчиков, поэтому аутентификация построена
по следующим правилам:

* пароль хранится хэшем argon2id — подобрать его по дампу базы нельзя;
* второй фактор — одноразовый код из приложения-аутентификатора;
* сессия хранится в базе, а не в подписанной куке: это позволяет
  отозвать её немедленно и не завершать сессии при перезапуске сервиса;
* каждая форма несёт токен CSRF, иначе чужой сайт мог бы отправить
  запрос от имени открытой сессии.
"""

import hashlib
import hmac
import secrets
from typing import Final

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from nosbp.core.constants import (
    ADMIN_SESSION_TOKEN_BYTES,
    CSRF_TOKEN_BYTES,
)

SESSION_COOKIE_NAME: Final[str] = "nosbp_admin"
CSRF_FIELD_NAME: Final[str] = "csrf_token"

TOTP_ISSUER: Final[str] = "NoSBP"
TOTP_VALID_WINDOW: Final = 1
"""Допуск в один шаг по 30 секунд в обе стороны — на расхождение часов."""

MIN_PASSWORD_LENGTH: Final = 12
"""Пароль администратора короче двенадцати символов не принимается."""

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


# ---------------------------------------------------------------------------
# Одноразовые коды
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """Создаёт секрет для приложения-аутентификатора."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    """Собирает ссылку otpauth:// для добавления в аутентификатор."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def verify_totp(secret: str, code: str) -> bool:
    """Проверяет одноразовый код.

    Допуск в один шаг компенсирует расхождение часов между телефоном
    и сервером — без него код на границе минуты не проходил бы.
    """
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=TOTP_VALID_WINDOW))


# ---------------------------------------------------------------------------
# Сессии и CSRF
# ---------------------------------------------------------------------------


def generate_session_token() -> str:
    """Создаёт токен сессии. Кладётся в куку и больше нигде не хранится."""
    return secrets.token_urlsafe(ADMIN_SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Считает хэш токена сессии — то, что лежит в базе."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_csrf_token() -> str:
    """Создаёт токен для форм."""
    return secrets.token_hex(CSRF_TOKEN_BYTES)


def csrf_tokens_match(expected: str, received: str | None) -> bool:
    """Сравнивает токены CSRF за постоянное время.

    Обычное сравнение строк выходит из цикла на первом различии, и по
    времени ответа токен можно подобрать посимвольно.
    """
    if not received:
        return False
    return hmac.compare_digest(expected, received)
