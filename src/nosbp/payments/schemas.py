"""Схемы данных для генерации платёжного QR-кода."""

from pydantic import BaseModel, ConfigDict, Field


class PayerInfo(BaseModel):
    """Данные плательщика.

    Эти поля попадают в строку ГОСТ, но НИКОГДА не сохраняются в базу —
    они участвуют только в вычислении ключа идемпотентности, то есть
    превращаются в необратимый хэш. Хранение персональных данных
    плательщиков — забота заказчика, а не сервиса.
    """

    model_config = ConfigDict(frozen=True)

    last_name: str | None = Field(default=None, max_length=160)
    first_name: str | None = Field(default=None, max_length=160)
    middle_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=25)

    @property
    def is_empty(self) -> bool:
        """Плательщик не указан вовсе."""
        return not any((self.last_name, self.first_name, self.middle_name, self.phone))


class PaymentRequest(BaseModel):
    """Динамическая часть запроса — то, что меняется от счёта к счёту."""

    model_config = ConfigDict(frozen=True)

    sum_kopecks: int | None = Field(
        default=None,
        ge=1,
        le=99_999_999_999,
        description="Сумма платежа в копейках, как того требует ГОСТ.",
    )
    purpose: str | None = Field(
        default=None,
        max_length=210,
        description="Назначение платежа.",
    )
    payer: PayerInfo = Field(default_factory=PayerInfo)


class PayeeRequisites(BaseModel):
    """Реквизиты получателя платежа.

    Собирается из модели ``Organization``. Валидация форматов происходит
    при сохранении организации, а не на каждом запросе — здесь схема нужна
    как typed-контракт между слоем данных и генератором строки ГОСТ.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=160)
    personal_acc: str = Field(pattern=r"^\d{20}$")
    bank_name: str = Field(min_length=1, max_length=45)
    bic: str = Field(pattern=r"^\d{9}$")
    corresp_acc: str = Field(pattern=r"^\d{20}$")
    payee_inn: str = Field(pattern=r"^\d{10}$|^\d{12}$")
    kpp: str | None = Field(default=None, pattern=r"^\d{9}$")
