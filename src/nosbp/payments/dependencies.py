"""Зависимости для модуля платежей."""


from fastapi import HTTPException, Query
from pydantic import ValidationError

from nosbp.payments.schemas import PaymentDetails


def get_payment_details(
    name: str = Query(..., description="Название получателя платежа"),
    personal_acc: str = Query(..., description="Расчётный счёт"),
    bank_name: str = Query(..., description="Наименование банка"),
    bic: str = Query(..., description="БИК банка"),
    payee_inn: str = Query(..., description="ИНН получателя"),
    corresp_acc: str | None = Query(None, description="Корр.счёт"),
    kpp: str | None = Query(None, description="КПП получателя"),
    payment_sum: int | None = Query(None, description="Сумма платежа в копейках"),
    purpose: str | None = Query(None, description="Назначение платежа"),
    first_name: str | None = Query(None, description="Имя плательщика"),
    last_name: str | None = Query(None, description="Фамилия плательщика"),
    middle_name: str | None = Query(None, description="Отчество плательщика"),
    phone: str | None = Query(None, description="Телефон плательщика"),
) -> PaymentDetails:
    try:
        return PaymentDetails(
            name=name,
            personal_acc=personal_acc,
            bank_name=bank_name,
            bic=bic,
            payee_inn=payee_inn,
            corresp_acc=corresp_acc,
            payment_sum=payment_sum,
            purpose=purpose,
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            phone=phone,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
