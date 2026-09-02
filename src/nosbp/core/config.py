"""Настройки приложения.

Значения читаются из переменных окружения или из файла ``.env`` рядом
с проектом. Всё, что может отличаться между локальной машиной, стендом
и продакшеном, живёт здесь — от адреса базы до стоимости счёта.

Константы, которые задаются стандартом и меняться не могут, лежат
в :mod:`nosbp.core.constants`.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nosbp.core.constants import (
    DEFAULT_QR_BORDER,
    DEFAULT_QR_SCALE,
    MAX_ACQUIRING_FEE_BPS,
)

LOCAL_ENVIRONMENT = "local"
"""Значение ``ENVIRONMENT``, при котором логи выводятся для человека."""


class Settings(BaseSettings):
    """Конфигурация сервиса."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- общее
    environment: str = Field(
        default=LOCAL_ENVIRONMENT,
        description="local | staging | production. Влияет на формат логов.",
    )
    log_level: str = Field(default="INFO")
    public_base_url: str = Field(
        default="https://nosbp.ru",
        description=(
            "Адрес, по которому сервис доступен снаружи. Из него собираются "
            "ссылки в документации и в подсказках консольной утилиты."
        ),
    )

    # ------------------------------------------------------------ база данных
    database_url: str = Field(
        default="postgresql+asyncpg://nosbp:nosbp@localhost:5432/nosbp",
        description="DSN PostgreSQL в формате SQLAlchemy.",
    )
    db_echo: bool = Field(default=False, description="Печатать SQL в логи.")

    # --------------------------------------------------- объектное хранилище
    # Здесь лежат логотипы организаций. Готовые QR-коды не хранятся:
    # повторный запрос содержит те же параметры, поэтому картинка просто
    # рисуется заново — это дешевле, чем хранить десятки тысяч файлов в сутки.
    s3_endpoint_url: str | None = Field(
        default=None,
        description="Адрес S3-совместимого хранилища. Пусто — настоящий AWS S3.",
    )
    s3_region: str = Field(default="ru-central1")
    s3_bucket: str = Field(default="nosbp-logos")
    s3_access_key: str = Field(default="")
    s3_secret_key: str = Field(default="")
    s3_connect_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        description=(
            "Таймаут подключения к хранилищу. Без него зависшее хранилище "
            "заблокировало бы поток, обслуживающий запрос."
        ),
    )
    s3_read_timeout_seconds: float = Field(default=5.0, gt=0)
    s3_max_attempts: int = Field(default=3, ge=1)

    # ------------------------------------------------------------ тарификация
    invoice_price_kopecks: int = Field(
        default=100,
        ge=0,
        description="Стоимость генерации одного счёта, в копейках.",
    )
    invoice_free_period_days: int = Field(
        default=30,
        ge=1,
        description=(
            "Сколько дней повторный запрос с теми же параметрами отдаётся "
            "бесплатно. После этого счёт считается новым и тарифицируется снова."
        ),
    )
    overdraft_days: int = Field(
        default=3,
        ge=0,
        description="Сколько дней сервис работает в долг после исчерпания баланса.",
    )
    overdraft_extension_days: int = Field(
        default=3,
        ge=0,
        description="На сколько дней заказчик может продлить овердрафт.",
    )
    overdraft_max_extensions: int = Field(
        default=1,
        ge=0,
        description="Сколько раз подряд можно продлить овердрафт.",
    )
    min_topup_kopecks: int = Field(
        default=100_000,
        ge=0,
        description="Минимальная сумма пополнения баланса, в копейках.",
    )
    default_daily_charge_limit_kopecks: int | None = Field(
        default=50_000,
        ge=0,
        description=(
            "Потолок списаний за сутки для новых аккаунтов — защита от "
            "выжигания баланса чужим ключом. Пусто — без ограничения."
        ),
    )
    billing_day_timezone: str = Field(
        default="Europe/Moscow",
        description=(
            "Часовой пояс, по которому начинаются сутки для суточного лимита. "
            "В UTC лимит сбрасывался бы среди рабочего дня заказчика."
        ),
    )

    # ------------------------------------------------------ комиссия эквайринга
    default_acquiring_fee_bps: int = Field(
        default=70,
        ge=0,
        le=MAX_ACQUIRING_FEE_BPS,
        description=(
            "Ставка эквайринга по умолчанию в базисных пунктах: 70 — это "
            "0,7 %. От неё считается, сколько заказчик сэкономил. У каждой "
            "организации ставку можно переопределить."
        ),
    )

    # --------------------------------------------------------------- админка
    admin_path_prefix: str = Field(
        default="/admin",
        description="По какому адресу отвечает панель управления.",
    )
    admin_session_ttl_hours: int = Field(
        default=12,
        ge=1,
        description="Сколько живёт сессия администратора без повторного входа.",
    )
    admin_session_idle_minutes: int = Field(
        default=60,
        ge=1,
        description="Через сколько минут без действий сессия закрывается.",
    )
    admin_max_login_attempts: int = Field(
        default=5,
        ge=1,
        description="Сколько неудачных попыток входа до временной блокировки.",
    )
    admin_lockout_minutes: int = Field(
        default=15,
        ge=1,
        description="На сколько минут блокируется вход после исчерпания попыток.",
    )
    admin_require_totp: bool = Field(
        default=True,
        description=(
            "Требовать одноразовый код из приложения-аутентификатора. "
            "Выключать стоит только на локальной машине."
        ),
    )
    admin_cookie_secure: bool = Field(
        default=True,
        description=(
            "Отдавать сессионную куку только по HTTPS. На локальной машине "
            "без сертификата придётся выключить, в проде — никогда."
        ),
    )

    # --------------------------------------------------------------- ключи
    token_last_used_throttle_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "Как часто обновлять отметку последнего использования ключа. "
            "Без ограничения это лишняя запись в базу на каждом запросе, "
            "а точность до секунды в кабинете никому не нужна."
        ),
    )

    # ------------------------------------------------------------- отрисовка
    qr_scale: int = Field(default=DEFAULT_QR_SCALE, ge=1, le=40)
    qr_border: int = Field(default=DEFAULT_QR_BORDER, ge=0, le=16)

    logo_cache_ttl_seconds: int = Field(
        default=600,
        ge=0,
        description="Сколько держать логотип организации в памяти процесса.",
    )
    logo_cache_max_entries: int = Field(
        default=512,
        ge=1,
        description=(
            "Предел числа логотипов в кэше. Без него кэш растёт вместе "
            "с числом организаций и однажды съест всю память."
        ),
    )

    @property
    def is_local(self) -> bool:
        """Локальная разработка — логи человекочитаемые, а не JSON."""
        return self.environment == LOCAL_ENVIRONMENT


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек.

    Кэшируется, чтобы ``.env`` читался один раз за время жизни процесса.
    В тестах кэш сбрасывается через ``get_settings.cache_clear()``.
    """
    return Settings()
