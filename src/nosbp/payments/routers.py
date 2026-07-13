"""Маршруты для модуля генерации qr-кодов."""

from typing import Annotated

from fastapi import APIRouter, Depends

from nosbp.payments.dependencies import get_payment_details
from nosbp.payments.gost import build_gost_payload
from nosbp.payments.schemas import PaymentDetails

router = APIRouter()


PaymentDetailsDep = Annotated[
    PaymentDetails,
    Depends(get_payment_details),

]


@router.get(
    path="/",
    summary="Получить QR-код",
    description="Получить QR-код для оплаты по реквизитам счёта.",
)
async def get_payment_qr(
    details: PaymentDetailsDep,
) -> str:
    """Возвращает изображение в png формате.

    Изображение – QR-код, сгенирированный по ГОСТ ГОСТ Р 56042-2014.
    """
    gost_text = build_gost_payload(details=details)
    return gost_text
