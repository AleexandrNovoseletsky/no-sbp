"""Второй фактор аутентификации администратора.

Пароли, сессионные токены и защита форм общие для панели и кабинета —
они находятся в :mod:`nosbp.core.security`. Здесь остаётся только то,
что относится к панели управления.
"""

from typing import Final

import pyotp

SESSION_COOKIE_NAME: Final[str] = "nosbp_admin"

TOTP_ISSUER: Final[str] = "NoSBP"
TOTP_VALID_WINDOW: Final = 1
"""Допуск в один шаг по 30 секунд в обе стороны — на расхождение часов."""


def generate_totp_secret() -> str:
    """Создаёт секрет для приложения-аутентификатора."""
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    """Собирает ссылку otpauth:// для добавления в аутентификатор."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def verify_totp(secret: str, code: str) -> bool:
    """Проверяет одноразовый код.

    Допуск в один шаг компенсирует расхождение часов между телефоном
    и сервером.
    """
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=TOTP_VALID_WINDOW))
