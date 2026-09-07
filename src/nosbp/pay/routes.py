"""Страница оплаты счёта и картинка QR-кода к ней.

Ни одна операция здесь не тарифицируется: счёт уже создан и оплачен
заказчиком в момент генерации, а плательщик просто смотрит на готовое.
"""

from typing import Final

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.errors import NosbpError
from nosbp.db.models import Invoice, Organization
from nosbp.pay.dependencies import PayTemplates
from nosbp.payments.dependencies import (
    AppSettings,
    DbSession,
    LogoCacheDep,
    requisites_of,
)
from nosbp.payments.gost import build_gost_payload
from nosbp.payments.qr import render_qr_png, validate_qr_color
from nosbp.payments.schemas import PayerInfo, PaymentRequest

router = APIRouter(tags=["pay"], include_in_schema=False)
log = structlog.get_logger()

NOT_FOUND: Final = 404

NO_STORE: Final[str] = "no-store"
"""Страница и картинка не кэшируются.

Кэш общего прокси хранил бы реквизиты чужого платежа, а выигрыш от него
здесь никакой: ссылку открывают считанные разы.
"""

NO_INDEX: Final[str] = "noindex, nofollow"
"""Ссылка попадает в письма и мессенджеры, откуда её собирают роботы.
Индексировать страницы с реквизитами не нужно."""

PAGE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": NO_STORE,
    "X-Robots-Tag": NO_INDEX,
}


async def _load(db: AsyncSession, token: str) -> tuple[Invoice, Organization] | None:
    """Находит счёт и организацию-получателя одним запросом."""
    result = await db.execute(
        select(Invoice, Organization)
        .join(Organization, Organization.id == Invoice.organization_id)
        .where(Invoice.public_token == token)
    )
    return result.first()  # type: ignore[return-value]


def _payload(invoice: Invoice, organization: Organization) -> str:
    """Собирает строку платежа по ГОСТ Р 56042-2014.

    Данные плательщика в строку не попадают: сервис их не хранит, они
    участвовали только в вычислении ключа идемпотентности. На оплату это
    не влияет — банк подставит имя владельца счёта, с которого платят.
    """
    payment = PaymentRequest(
        sum_kopecks=invoice.sum_kopecks,
        purpose=invoice.purpose,
        payer=PayerInfo(),
    )
    return build_gost_payload(requisites_of(organization), payment)


@router.get("/{token}")
async def payment_page(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: PayTemplates,
    token: str,
) -> Response:
    """Показывает счёт: сумму, назначение, QR-код и реквизиты текстом."""
    found = await _load(db, token)
    if found is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"settings": settings},
            status_code=NOT_FOUND,
            headers=PAGE_HEADERS,
        )

    invoice, organization = found
    return templates.TemplateResponse(
        request,
        "invoice.html",
        {
            "settings": settings,
            "invoice": invoice,
            "organization": organization,
            "requisites": requisites_of(organization),
        },
        headers=PAGE_HEADERS,
    )


@router.get("/{token}/qr.png")
async def payment_qr(
    db: DbSession,
    settings: AppSettings,
    logo_cache: LogoCacheDep,
    token: str,
) -> Response:
    """Отдаёт QR-код счёта картинкой.

    Картинка рисуется заново на каждый запрос: готовые PNG сервис не
    хранит, а отрисовка занимает миллисекунды.
    """
    found = await _load(db, token)
    if found is None:
        return Response(status_code=NOT_FOUND, headers=PAGE_HEADERS)

    invoice, organization = found
    try:
        payload = _payload(invoice, organization)
        color = validate_qr_color(organization.qr_color)
    except NosbpError as error:
        # Реквизиты сломались уже после создания счёта — например, их
        # правили в кабинете. Плательщику показывать нечего.
        log.warning("pay_qr_broken", invoice=token, message=error.message)
        return Response(status_code=NOT_FOUND, headers=PAGE_HEADERS)

    return Response(
        content=render_qr_png(
            payload,
            logo=await logo_cache.get(organization.logo_key),
            color=color,
            scale=settings.qr_scale,
            border=settings.qr_border,
        ),
        media_type="image/png",
        headers=PAGE_HEADERS,
    )
