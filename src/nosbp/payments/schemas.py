"""Схемы данных для генерации платёжного QR-кода.

Ограничения длин берутся из :mod:`nosbp.core.constants` — из того же
места, что и колонки базы и описания HTTP-параметров.
"""

from pydantic import BaseModel, ConfigDict, Field

from nosbp.core.constants import (
    ACCOUNT_PATTERN,
    BANK_NAME_MAX_LENGTH,
    BIC_PATTERN,
    INN_PATTERN,
    KPP_PATTERN,
    MAX_SUM_KOPECKS,
    NAME_MAX_LENGTH,
    PHONE_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
)


class PayerInfo(BaseModel):
    """Данные плательщика.

    Эти поля попадают в строку ГОСТ, но НИКОГДА не сохраняются в базу —
    они участвуют только в вычислении ключа идемпотентности, то есть
    превращаются в необратимый хэш. Хранение персональных данных
    плательщиков — забота заказчика, а не сервиса.
    """

    model_config = ConfigDict(frozen=True)

    last_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    first_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    middle_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    phone: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH)


class PaymentRequest(BaseModel):
    """Динамическая часть запроса — то, что меняется от счёта к счёту."""

    model_config = ConfigDict(frozen=True)

    sum_kopecks: int | None = Field(
        default=None,
        ge=1,
        le=MAX_SUM_KOPECKS,
        description="Сумма платежа в копейках, как того требует ГОСТ.",
    )
    purpose: str | None = Field(
        default=None,
        max_length=PURPOSE_MAX_LENGTH,
        description="Назначение платежа.",
    )
    payer: PayerInfo = Field(default_factory=PayerInfo)


class PayeeRequisites(BaseModel):
    """Реквизиты получателя платежа.

    Собирается из модели ``Organization``. Контрольные разряды проверяются
    при сохранении организации, а не на каждом запросе — здесь схема нужна
    как типизированный контракт между слоем данных и генератором строки ГОСТ.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    personal_acc: str = Field(pattern=ACCOUNT_PATTERN)
    bank_name: str = Field(min_length=1, max_length=BANK_NAME_MAX_LENGTH)
    bic: str = Field(pattern=BIC_PATTERN)
    corresp_acc: str = Field(pattern=ACCOUNT_PATTERN)
    payee_inn: str = Field(pattern=INN_PATTERN)
    kpp: str | None = Field(default=None, pattern=KPP_PATTERN)
