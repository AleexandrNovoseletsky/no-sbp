"""Схемы для генерации платежей."""

from pydantic import BaseModel, Field


class PaymentDetails(BaseModel):
    """Получатель платежа."""

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

    payee_inn: str = Field(
        description="ИНН получателя",
        pattern=r"^\d{10}$|^\d{12}$",
    )

    corresp_acc: str | None = Field(
        description="Корр.счёт",
        default=None,
        pattern=r"^\d{20}$",
    )

    kpp: str | None = Field(
        description="КПП получателя",
        default=None,
        pattern=r"^\d{9}$",
    )

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
        max_length=25
    )
