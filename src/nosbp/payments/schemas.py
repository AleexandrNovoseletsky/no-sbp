"""Схемы для генерации платежей."""

from enum import StrEnum
from pydantic import BaseModel, Field


class QrErrorCorrectionLevel(StrEnum):
    """Уровни исправления QR-кода."""

    LOW = "L"
    MEDIUM = "M"
    QUARTILE = "Q"
    HIGH = "H"

class PaymentQrRequest(BaseModel):
    """Данные для генерации QR-кода оплаты."""

    name: str = Field(
        description="Получатель платежа",
        min_length=1, max_length=160,
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

    amount: int = Field(
        description="Сумма платежа в копейках",
        gt=0,
    )

    purpose: str | None = Field(
        description="Назначение платежа",
        default=None,
        max_length=210,
    )

    qr_error_correction_level: QrErrorCorrectionLevel = Field(
        description="Уровень исправления QR-кода",
        default=QrErrorCorrectionLevel.MEDIUM,
    )

