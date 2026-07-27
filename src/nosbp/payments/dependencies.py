"""Зависимости для эндпоинтов оплаты."""

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.db.database import get_db
from nosbp.db.models import Client
from nosbp.payments.schemas import PaymentDetails


async def get_payment_details(
    api_key: str = Query(..., description="API-ключ клиента"),
    payment_sum: int | None = Query(
        None, description="Сумма платежа в копейках"
    ),
    purpose: str | None = Query(None, description="Назначение платежа"),
    first_name: str | None = Query(None, description="Имя плательщика"),
    last_name: str | None = Query(None, description="Фамилия плательщика"),
    middle_name: str | None = Query(None, description="Отчество плательщика"),
    phone: str | None = Query(None, description="Телефон плательщика"),
    db: AsyncSession = Depends(get_db),
) -> PaymentDetails:
    """Находит клиента по api_key и собирает полные данные для генерации QR."""
    result = await db.execute(select(Client).where(Client.api_key == api_key))
    client = result.scalar_one_or_none()

    if client is None or not client.is_active:
        raise HTTPException(
            status_code=401, detail="Неверный или неактивный api_key"
        )

    return PaymentDetails(
        # Статика — из БД, по найденному клиенту
        name=client.name,
        personal_acc=client.personal_acc,
        bank_name=client.bank_name,
        bic=client.bic,
        corresp_acc=client.corresp_acc,
        payee_inn=client.payee_inn,
        # Динамика — из query-параметров запроса
        payment_sum=payment_sum,
        purpose=purpose,
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        phone=phone,
    )
