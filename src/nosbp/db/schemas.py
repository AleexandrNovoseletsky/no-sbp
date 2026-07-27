"""Схемы для генерации платежей."""

from pydantic import BaseModel, Field


class ClientPaymentProfile(BaseModel):
    """Статичные реквизиты получателя — хранятся в БД, привязаны к api_key."""

    name: str = Field(
        description="Название получателя платежа",
        min_length=1,
        max_length=160,
    )
    personal_acc: str = Field(
        description="Расчётный счёт",
        pattern=r"^\d{20}$",
    )
    bank_name: str = Field(
        description="Наименование банка",
        min_length=1,
        max_length=45,
    )
    bic: str = Field(
        description="БИК банка",
        pattern=r"^\d{9}$",
    )
    corresp_acc: str = Field(
        description="Корр.счёт",
        pattern=r"^\d{20}$",
    )
    payee_inn: str = Field(
        description="ИНН получателя платежа",
        pattern=r"^\d{10}$|^\d{12}$",
    )


class PaymentRequest(BaseModel):
    """Динамические данные конкретного запроса — приходят в каждом запросе."""

    payment_sum: int | None = Field(
        description="Сумма платежа в копейках",
        default=None,
        ge=1,
    )
    purpose: str | None = Field(
        description="Назначение платежа",
        default=None,
        max_length=210,
    )
    first_name: str | None = Field(
        description="Имя плательщика",
        default=None,
        max_length=160,
    )
    last_name: str | None = Field(
        description="Фамилия плательщика",
        default=None,
        max_length=160,
    )
    middle_name: str | None = Field(
        description="Отчество плательщика",
        default=None,
        max_length=160,
    )
    phone: str | None = Field(
        description="Телефон плательщика",
        default=None,
        max_length=25,
    )


class PaymentDetails(ClientPaymentProfile, PaymentRequest):
    """Полный набор данных для построения ST00012 payload.

    Собирается вручную в dependencies.py: статика — из БД по api_key,
    динамика — из query-параметров запроса.
    """

    pass
