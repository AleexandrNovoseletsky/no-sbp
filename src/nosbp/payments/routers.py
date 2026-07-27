"""Маршруты для модуля генерации qr-кодов."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response

from payments.dependencies import get_payment_details
from payments.gost import build_gost_payload
from payments.qr import render_qr_png
from payments.schemas import PaymentDetails

router = APIRouter()
log = structlog.get_logger()


PaymentDetailsDep = Annotated[
    PaymentDetails,
    Depends(get_payment_details),

]


@router.get(
    path="/",
    summary="Получить QR-код",
    description="Получить QR-код для оплаты по реквизитам счёта.",
    response_class=Response,
)
async def get_payment_qr(
    details: PaymentDetailsDep,
) -> Response:
    """Возвращает изображение в png формате.

    Изображение – QR-код, сгенирированный по ГОСТ ГОСТ Р 56042-2014.
    """
    log.info(
        "request_received", recipient=details.name, paymant_amount=details.payment_sum
    )
    gost_text = build_gost_payload(details=details)
    png_bytes = render_qr_png(payload=gost_text)
    return Response(content=png_bytes, media_type="image/png")

