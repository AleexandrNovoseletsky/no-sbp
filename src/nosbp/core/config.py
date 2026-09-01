"""Настройки приложения.

Все значения читаются из переменных окружения (или из файла .env рядом с
проектом). Ни одна настройка не зашита в код — это позволяет держать один
и тот же образ для локальной разработки и для продакшена.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация сервиса."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- общее
    environment: str = Field(
        default="local",
        description="local | staging | production. Влияет на формат логов.",
    )
    log_level: str = Field(default="INFO")

    # ------------------------------------------------------------ база данных
    database_url: str = Field(
        default="postgresql+asyncpg://nosbp:nosbp@localhost:5432/nosbp",
        description="DSN PostgreSQL в формате SQLAlchemy.",
    )
    db_echo: bool = Field(default=False, description="Печатать SQL в логи.")

    # -------------------------------------------------------- объектное хранилище
    # Здесь лежат логотипы организаций. Готовые QR-коды НЕ хранятся:
    # повторный запрос содержит те же параметры, поэтому картинка просто
    # рисуется заново — это дешевле, чем хранить десятки тысяч файлов в сутки.
    s3_endpoint_url: str | None = Field(
        default=None,
        description="Адрес S3-совместимого хранилища. None — настоящий AWS S3.",
    )
    s3_region: str = Field(default="ru-central1")
    s3_bucket: str = Field(default="nosbp-logos")
    s3_access_key: str = Field(default="")
    s3_secret_key: str = Field(default="")

    # ------------------------------------------------------------ тарификация
    invoice_price_kopecks: int = Field(
        default=100,
        ge=0,
        description="Стоимость генерации одного счёта. По умолчанию 1 рубль.",
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
        description="На сколько дней заказчик может продлить овердрафт из кабинета.",
    )
    overdraft_max_extensions: int = Field(
        default=1,
        ge=0,
        description="Сколько раз подряд можно продлить овердрафт.",
    )
    min_topup_kopecks: int = Field(
        default=100_000,
        ge=0,
        description="Минимальная сумма пополнения баланса. По умолчанию 1000 рублей.",
    )
    default_daily_charge_limit_kopecks: int | None = Field(
        default=50_000,
        description=(
            "Потолок списаний за сутки для новых аккаунтов, защита от "
            "выжигания баланса чужим ключом. None — без ограничения."
        ),
    )

    # ------------------------------------------------------------- отрисовка
    qr_scale: int = Field(default=10, ge=1, le=40)
    qr_border: int = Field(default=4, ge=0, le=16)

    @property
    def is_local(self) -> bool:
        """Локальная разработка — логи человекочитаемые, а не JSON."""
        return self.environment == "local"


@lru_cache
def get_settings() -> Settings:
    """Возвращает singleton настроек.

    Кэшируется, чтобы .env читался один раз за время жизни процесса.
    В тестах кэш сбрасывается через ``get_settings.cache_clear()``.
    """
    return Settings()
