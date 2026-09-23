"""Настройки упрощённого сервиса.

Всё читается из переменных окружения: панели управления здесь нет,
и менять настройки на ходу некому. Реквизиты проверяются при старте —
опечатка в номере счёта должна всплыть при развёртывании, а не в тот
момент, когда клиент не сможет заплатить.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from nosbp.core.constants import DEFAULT_QR_BORDER, DEFAULT_QR_COLOR, DEFAULT_QR_SCALE
from nosbp.core.errors import NosbpError
from nosbp.payments.qr import validate_qr_color
from nosbp.payments.requisites import (
    validate_account,
    validate_bic,
    validate_inn,
    validate_kpp,
)
from nosbp.payments.schemas import PayeeRequisites

SECONDS_PER_DAY: Final = 24 * 60 * 60

DEFAULT_ORG_ALIAS: Final[str] = "main"
"""Значение параметра ``org``, которое сервис соглашается обслуживать.

Организация здесь одна, но параметр остаётся ради совместимости
с шаблонами CRM, где он уже проставлен.
"""

DEFAULT_CACHE_DAYS: Final = 30
LOGO_MAX_BYTES: Final = 512 * 1024
MIN_TOKEN_LENGTH: Final = 16
"""Короткий ключ в адресе картинки подбирается перебором за вечер."""


class ConfigError(RuntimeError):
    """Сервис настроен так, что работать не может.

    Поднимается только при старте: дальше настройки не меняются.
    """


@dataclass(frozen=True, slots=True)
class Settings:
    """Проверенная конфигурация сервиса."""

    token: str
    requisites: PayeeRequisites
    org_alias: str
    qr_color: str
    qr_scale: int
    qr_border: int
    logo: bytes | None
    cache_max_age_seconds: int


def _required(name: str) -> str:
    """Читает обязательную переменную окружения.

    :raises ConfigError: переменная не задана или пуста.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана обязательная переменная {name}.")
    return value


def _optional(name: str, default: str = "") -> str:
    """Читает необязательную переменную окружения."""
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Читает целочисленную переменную с проверкой границ.

    :raises ConfigError: значение не число или вне допустимого диапазона.
    """
    raw = _optional(name)
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидается целое число, получено «{raw}».") from None

    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{name}: значение {value} вне диапазона {minimum}–{maximum}."
        )
    return value


def _load_token() -> str:
    """Читает ключ доступа.

    Ключ обязателен: без него адрес картинки открыт всему интернету,
    а нарисованный по нему QR-код ведёт на ваш расчётный счёт с любым
    назначением платежа, какое подставит посторонний.

    :raises ConfigError: ключ не задан или слишком короткий.
    """
    token = _required("QR_TOKEN")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"QR_TOKEN короче {MIN_TOKEN_LENGTH} символов. "
            "Сгенерируйте длинный: openssl rand -base64 32"
        )
    return token


def _load_requisites() -> PayeeRequisites:
    """Собирает и проверяет реквизиты получателя.

    Контрольные разряды счетов и ИНН считаются здесь же: реквизиты
    задаются один раз при развёртывании, и проверять их на каждом
    запросе незачем.

    :raises ConfigError: какой-то из реквизитов не проходит проверку.
    """
    try:
        bic = validate_bic(_required("PAYEE_BIC"))
        return PayeeRequisites(
            name=_required("PAYEE_NAME"),
            personal_acc=validate_account(_required("PAYEE_ACCOUNT"), bic),
            bank_name=_required("PAYEE_BANK_NAME"),
            bic=bic,
            corresp_acc=validate_account(_required("PAYEE_CORR_ACCOUNT"), bic),
            payee_inn=validate_inn(_required("PAYEE_INN")),
            kpp=validate_kpp(_optional("PAYEE_KPP") or None),
        )
    except NosbpError as error:
        raise ConfigError(f"Реквизиты получателя: {error.message}") from error


def _load_logo() -> bytes | None:
    """Читает логотип с диска один раз при старте.

    Файл монтируется в контейнер и не меняется, поэтому держать его
    в памяти дешевле, чем перечитывать на каждом запросе.

    :raises ConfigError: файла нет или он слишком велик.
    """
    configured = _optional("LOGO_PATH")
    if not configured:
        return None

    path = Path(configured)
    if not path.is_file():
        raise ConfigError(f"LOGO_PATH: файл {path} не найден внутри контейнера.")

    data = path.read_bytes()
    if len(data) > LOGO_MAX_BYTES:
        raise ConfigError(
            f"LOGO_PATH: файл {path} занимает {len(data) // 1024} КБ "
            f"при пределе {LOGO_MAX_BYTES // 1024} КБ. "
            "В центре QR-кода он занимает меньше сотни пикселей."
        )
    return data


def _load_color() -> str:
    """Читает цвет модулей QR-кода.

    :raises ConfigError: цвет слишком светлый для сканирования.
    """
    try:
        return validate_qr_color(_optional("QR_COLOR", DEFAULT_QR_COLOR))
    except NosbpError as error:
        raise ConfigError(f"QR_COLOR: {error.message}") from error


def load_settings() -> Settings:
    """Собирает настройки из окружения и проверяет их.

    :raises ConfigError: любая настройка задана неверно. Сервис не
        поднимается: работать с неверными реквизитами хуже, чем не
        работать вовсе.
    """
    return Settings(
        token=_load_token(),
        requisites=_load_requisites(),
        org_alias=_optional("ORG_ALIAS", DEFAULT_ORG_ALIAS),
        qr_color=_load_color(),
        qr_scale=_int("QR_SCALE", DEFAULT_QR_SCALE, minimum=1, maximum=40),
        qr_border=_int("QR_BORDER", DEFAULT_QR_BORDER, minimum=0, maximum=16),
        logo=_load_logo(),
        cache_max_age_seconds=_int(
            "CACHE_MAX_AGE_DAYS", DEFAULT_CACHE_DAYS, minimum=0, maximum=365
        )
        * SECONDS_PER_DAY,
    )
