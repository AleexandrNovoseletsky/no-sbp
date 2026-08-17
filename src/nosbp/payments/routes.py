"""Маршруты для модуля генерации qr-кодов."""

from typing import Annotated

import secrets
import structlog
from fastapi import APIRouter, Depends, Response, Body, status, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from src.nosbp.payments.dependencies import get_payment_details
from src.nosbp.payments.gost import build_gost_payload
from src.nosbp.payments.qr import render_qr_png
from src.nosbp.payments.schemas import PaymentDetails
from src.nosbp.db.database import get_db
from src.nosbp.db.models import Client

router = APIRouter()
log = structlog.get_logger()


PaymentDetailsDep = Annotated[
    PaymentDetails,
    Depends(get_payment_details),
]


class PaymentRequest(BaseModel):
    """Body для POST-запроса генерации QR через JSON.

    Содержит api_key для идентификации клиента и опциональные поля,
    которые раньше передавались как query-параметры.
    """

    api_key: str
    payment_sum: int | None = None
    purpose: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    phone: str | None = None


class ClientCreate(BaseModel):
    display_name: str
    name: str
    personal_acc: str
    bank_name: str
    bic: str
    corresp_acc: str | None = None
    payee_inn: str


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
        "request_received",
        recipient=details.name,
        paymant_amount=details.payment_sum,
    )
    gost_text = build_gost_payload(details=details)
    png_bytes = render_qr_png(payload=gost_text)
    return Response(content=png_bytes, media_type="image/png")


@router.post(
    path="/",
    summary="Сгенерировать QR-код (POST)",
    description="Генерация QR-кода через POST с телом JSON. Сохраняет поведение GET.",
    response_class=Response,
)
async def post_payment_qr(
    payload: PaymentRequest = Body(..., description="Данные для генерации QR, включая api_key"),
    db=Depends(get_db),
) -> Response:
    """POST-реализация генерации QR. Поддерживает те же поля, что и GET через query.

    Принимает api_key в теле запроса для идентификации клиента.
    """
    # Найти клиента по api_key
    result = await db.execute(select(Client).where(Client.api_key == payload.api_key))
    client = result.scalar_one_or_none()

    if client is None or not client.is_active:
        raise HTTPException(status_code=401, detail="Неверный или неактивный api_key")

    details = PaymentDetails(
        name=client.name,
        personal_acc=client.personal_acc,
        bank_name=client.bank_name,
        bic=client.bic,
        corresp_acc=client.corresp_acc,
        payee_inn=client.payee_inn,
        payment_sum=payload.payment_sum,
        purpose=payload.purpose,
        first_name=payload.first_name,
        last_name=payload.last_name,
        middle_name=payload.middle_name,
        phone=payload.phone,
    )

    log.info(
        "request_received",
        recipient=details.name,
        paymant_amount=details.payment_sum,
        method="POST",
    )

    gost_text = build_gost_payload(details=details)
    png_bytes = render_qr_png(payload=gost_text)
    return Response(content=png_bytes, media_type="image/png")


@router.post(
    path="/register",
    summary="Зарегистрировать нового клиента",
    description="Регистрация клиента сервиса. Возвращает сгенерированный api_key.",
    response_class=JSONResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_client(
    client_data: ClientCreate = Body(..., description="Данные клиента"),
    db=Depends(get_db),
) -> JSONResponse:
    """Создаёт нового клиента и возвращает api_key.

    Простейшая регистрация — без аутентификации администратора. Для продакшена
    следует ограничить доступ к этому эндпоинту.
    """
    api_key = secrets.token_urlsafe(32)

    client = Client(
        api_key=api_key,
        display_name=client_data.display_name,
        name=client_data.name,
        personal_acc=client_data.personal_acc,
        bank_name=client_data.bank_name,
        bic=client_data.bic,
        corresp_acc=client_data.corresp_acc or "",
        payee_inn=client_data.payee_inn,
    )

    db.add(client)
    await db.commit()

    return JSONResponse(content={"api_key": api_key}, status_code=status.HTTP_201_CREATED)
