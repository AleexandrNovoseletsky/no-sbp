"""Настройки приложения.

Значения читаются из переменных окружения или из файла ``.env`` рядом
с проектом. Всё, что может отличаться между локальной машиной, стендом
и продакшеном, живёт здесь — от адреса базы до стоимости счёта.

Константы, которые задаются стандартом и меняться не могут, лежат
в :mod:`nosbp.core.constants`.
"""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from nosbp.core.constants import (
    DEFAULT_ACQUIRING_FEE_BPS,
    DEFAULT_QR_BORDER,
    DEFAULT_QR_SCALE,
    MAX_ACQUIRING_FEE_BPS,
    SERVICE_NAME,
    SMTP_DEFAULT_PORTS,
    SmtpSecurity,
)

LOCAL_ENVIRONMENT = "local"
PRODUCTION_ENVIRONMENT = "production"


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
    support_email: str = Field(
        default="",
        description=(
            "Адрес поддержки. Показывается заказчику, когда действие "
            "требует участия оператора — например, восстановление пароля."
        ),
    )
    public_base_url: str = Field(
        default="http://127.0.0.1:8080",
        description=(
            "Внешний адрес сервиса вместе со схемой и портом. Используется "
            "для сборки ссылок в выводе консольных команд и в документации. "
            "В рабочем окружении задаётся обязательно."
        ),
    )
    enable_api_docs: bool | None = Field(
        default=None,
        description=(
            "Отдавать ли /docs, /redoc и /openapi.json. По умолчанию "
            "включено везде, кроме production."
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
        default=DEFAULT_ACQUIRING_FEE_BPS,
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
        default="/console",
        min_length=2,
        pattern=r"^/[A-Za-z0-9_\-/]*[A-Za-z0-9_\-]$",
        description=(
            "Путь, по которому отвечает панель управления. В рабочем "
            "окружении задаётся собственным непубличным значением: это не "
            "средство защиты, но отсекает сканеры и шум в логах."
        ),
    )
    admin_allowed_networks: str = Field(
        default="",
        description=(
            "Список сетей в формате CIDR через запятую, которым разрешён "
            "доступ к панели. Пустое значение — проверка не выполняется "
            "(ограничение задаётся на уровне обратного прокси)."
        ),
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

    # ------------------------------------------------------- личный кабинет
    cabinet_path_prefix: str = Field(
        default="/cabinet",
        min_length=2,
        pattern=r"^/[A-Za-z0-9_\-/]*[A-Za-z0-9_\-]$",
        description="Путь, по которому доступен личный кабинет заказчика.",
    )
    cabinet_registration_open: bool = Field(
        default=True,
        description=(
            "Разрешена ли самостоятельная регистрация. При закрытой "
            "регистрации учётные записи заводит оператор через панель."
        ),
    )
    cabinet_session_ttl_hours: int = Field(default=24, ge=1)
    cabinet_session_idle_minutes: int = Field(default=180, ge=1)
    cabinet_invite_ttl_hours: int = Field(
        default=72,
        ge=1,
        description=(
            "Сколько действует ссылка для установки пароля. Ссылка "
            "одноразовая и выдаётся оператором."
        ),
    )
    cabinet_max_login_attempts: int = Field(default=10, ge=1)
    cabinet_lockout_minutes: int = Field(default=15, ge=1)
    cabinet_require_email_confirmation: bool = Field(
        default=False,
        description=(
            "Требовать подтверждение адреса почты перед входом. Включать "
            "стоит только когда отправка писем настроена и проверена: "
            "иначе новые заказчики останутся без доступа."
        ),
    )

    # ------------------------------------------------- восстановление пароля
    password_reset_ttl_hours: int = Field(
        default=1,
        ge=1,
        description=(
            "Сколько действует ссылка, запрошенная заказчиком самостоятельно. "
            "Короче, чем у ссылки от оператора: эту никто не проверяет "
            "глазами, и запросить её может кто угодно, зная адрес."
        ),
    )
    password_reset_max_per_hour: int = Field(
        default=3,
        ge=1,
        description=(
            "Сколько писем о восстановлении можно запросить на один адрес "
            "за час. Ограничение бережёт чужой почтовый ящик от заваливания "
            "и нашу репутацию отправителя от жалоб на спам."
        ),
    )
    email_confirm_ttl_hours: int = Field(
        default=48,
        ge=1,
        description="Сколько действует ссылка подтверждения адреса почты.",
    )

    # -------------------------------------------------------- страница оплаты
    payment_page_enabled: bool = Field(
        default=False,
        description=(
            "Отдавать ли публичную страницу оплаты по ссылке из счёта. "
            "Пока расчётный счёт не открыт, страницу показывать нельзя: "
            "она раскрывает реквизиты получателя. Выключено — маршруты "
            "не регистрируются вовсе, адрес отвечает 404."
        ),
    )
    payment_page_path_prefix: str = Field(
        default="/pay",
        min_length=2,
        pattern=r"^/[A-Za-z0-9_\-/]*[A-Za-z0-9_\-]$",
        description="Путь публичной страницы оплаты.",
    )

    # ---------------------------------------------------------- оповещения
    telegram_bot_token: str = Field(
        default="",
        description="Токен бота Telegram от @BotFather.",
    )
    telegram_chat_id: str = Field(
        default="",
        description="Идентификатор чата, куда бот отправляет оповещения.",
    )
    max_bot_token: str = Field(
        default="",
        description="Токен бота мессенджера MAX.",
    )
    max_chat_id: str = Field(
        default="",
        description="Идентификатор чата MAX для оповещений.",
    )
    notification_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Таймаут запроса к API мессенджера.",
    )
    notify_admin_login: bool = Field(
        default=True,
        description="Оповещать об успешном входе в панель управления.",
    )
    notify_admin_login_failed: bool = Field(
        default=True,
        description=(
            "Оповещать о неудачных попытках входа. Позволяет заметить "
            "подбор пароля, но при активном сканировании даёт много "
            "сообщений."
        ),
    )

    # ------------------------------------------------------------------ почта
    # Письма нужны для восстановления пароля и выдачи доступа в кабинет.
    # Пока настройки пусты, письма не отправляются, а их текст пишется
    # в журнал: сервис работает, но доступ выдаётся ссылкой из панели.
    smtp_host: str = Field(
        default="",
        description="Адрес почтового сервера, например smtp.yandex.ru.",
    )
    smtp_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description=(
            "Порт почтового сервера. Ноль — взять обычный порт выбранного "
            "режима: 465 для ssl, 587 для starttls."
        ),
    )
    smtp_user: str = Field(default="", description="Логин на почтовом сервере.")
    smtp_password: str = Field(
        default="",
        description=(
            "Пароль почтового ящика. Для Яндекса и большинства провайдеров "
            "это пароль приложения, а не пароль от аккаунта."
        ),
    )
    smtp_security: SmtpSecurity = Field(
        default=SmtpSecurity.SSL,
        description=(
            "Как защищается соединение: ssl (порт 465), starttls (587) "
            "или none — только для релея на том же хосте."
        ),
    )
    smtp_from: str = Field(
        default="",
        description=(
            "Адрес в поле «От кого». Пусто — берётся логин. Должен "
            "принадлежать домену, для которого настроены SPF и DKIM, "
            "иначе письма уйдут в спам."
        ),
    )
    smtp_from_name: str = Field(
        default=SERVICE_NAME,
        description="Имя отправителя рядом с адресом.",
    )
    smtp_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description=(
            "Таймаут отправки письма. Письма уходят фоновой задачей, "
            "но зависшее соединение всё равно занимает ресурсы процесса."
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

    logo_max_bytes: int = Field(
        default=512 * 1024,
        gt=0,
        description=(
            "Предельный размер файла логотипа. В QR-коде он занимает меньше "
            "сотни пикселей, поэтому мегабайты здесь ни к чему."
        ),
    )
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
        """Признак локальной разработки: влияет на формат логов."""
        return self.environment == LOCAL_ENVIRONMENT

    @property
    def is_production(self) -> bool:
        """Признак рабочего окружения."""
        return self.environment == PRODUCTION_ENVIRONMENT

    @property
    def show_api_docs(self) -> bool:
        """Нужно ли отдавать интерактивную документацию API."""
        if self.enable_api_docs is not None:
            return self.enable_api_docs
        return not self.is_production

    @property
    def admin_prefix(self) -> str:
        """Путь панели без завершающего слэша."""
        return self.admin_path_prefix.rstrip("/")

    @property
    def cabinet_prefix(self) -> str:
        """Путь личного кабинета без завершающего слэша."""
        return self.cabinet_path_prefix.rstrip("/")

    @property
    def payment_prefix(self) -> str:
        """Путь публичной страницы оплаты без завершающего слэша."""
        return self.payment_page_path_prefix.rstrip("/")

    @property
    def mail_from(self) -> str:
        """Адрес отправителя писем."""
        return self.smtp_from or self.smtp_user

    @property
    def mail_from_name(self) -> str:
        """Имя отправителя писем."""
        return self.smtp_from_name or SERVICE_NAME

    @property
    def smtp_effective_port(self) -> int:
        """Порт почтового сервера с учётом выбранного режима."""
        return self.smtp_port or SMTP_DEFAULT_PORTS[self.smtp_security]

    @property
    def mail_configured(self) -> bool:
        """Настроена ли отправка писем."""
        return bool(self.smtp_host and self.mail_from)

    @property
    def admin_networks(self) -> tuple[IPv4Network | IPv6Network, ...]:
        """Разобранный список разрешённых сетей.

        :raises ValueError: если сеть записана некорректно.
        """
        return tuple(
            ip_network(item.strip(), strict=False)
            for item in self.admin_allowed_networks.split(",")
            if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Возвращает единственный экземпляр настроек.

    Кэшируется, чтобы ``.env`` читался один раз за время жизни процесса.
    В тестах кэш сбрасывается через ``get_settings.cache_clear()``.
    """
    return Settings()
