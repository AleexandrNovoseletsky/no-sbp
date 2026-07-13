"""Схемы для генерации платежей."""

from enum import StrEnum

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

    corresp_acc: str | None = Field(
        description="Корр.счёт",
        default=None,
        pattern=r"^\d{20}$",
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
    
    midle_name: str | None = Field(
        description="Отчество плательщика",
        default=None,
        max_length=160,
    )

    phone: str | None = Field(
        description="Телефон плательщика",
        default=None,
        max_length=25
    )

class QrErrorCorrectionLevel(StrEnum):
    """Уровни исправления QR-кода."""

    LOW = "L"
    MEDIUM = "M"
    QUARTILE = "Q"
    HIGH = "H"


class PaymentQrRequest(BaseModel):
    """Данные для генерации QR-кода оплаты."""

    recipient: PaymentDetails

    additional_fields: dict[str, str] | None = Field(
        description="Дополнительные поля",
        default_factory=dict,
    )

    qr_error_correction_level: QrErrorCorrectionLevel = Field(
        description="Уровень исправления QR-кода",
        default=QrErrorCorrectionLevel.MEDIUM,
    )

