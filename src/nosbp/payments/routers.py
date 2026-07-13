"""Маршруты для модуля генерации qr-кодов."""

from fastapi import APIRouter, Depends

from nosbp.payments.dependencies import get_payment_details
from nosbp.payments.schemas import PaymentDetails


router = APIRouter()


@router.get(
    path="/",
    summary="Получить QR-код",
    description="Получить QR-код для оплаты по реквизитам счёта.",
)
async def get_payment_qr(
    details: PaymentDetails = Depends(get_payment_details),
):
    """Возвращает изображение в png формате.
    
    Изображение – QR-код, сгенирированный по ГОСТ ГОСТ Р 56042-2014.
    """
    return {"test": "OK"}
