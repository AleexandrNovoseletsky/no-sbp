"""HTTP-эндпоинты генерации платёжного QR-кода."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Body, Header, Query, Request, Response
from pydantic import BaseModel, Field

from nosbp.core.errors import NosbpError
from nosbp.db.models import Account, Organization
from nosbp.invoices.service import InvoiceService
from nosbp.payments.dependencies import (
    AppSettings,
    DbSession,
    LogoCacheDep,
    authenticate,
    resolve_organization,
)
from nosbp.payments.gost import build_gost_payload
from nosbp.payments.placeholder import render_placeholder_png
from nosbp.payments.qr import render_qr_png, validate_qr_color
from nosbp.payments.schemas import PayeeRequisites, PayerInfo, PaymentRequest

router = APIRouter(tags=["qr"])
log = structlog.get_logger()

ERROR_HEADER = "X-NoSBP-Error"
"""Машиночитаемый код ошибки, когда в теле ответа лежит картинка-заглушка."""


class QrRequestBody(BaseModel):
    """Тело POST-запроса на генерацию QR-кода."""

    org: str | None = Field(
        default=None, description="Алиас организации-получателя платежа."
    )
    sum: int | None = Field(default=None, ge=1, description="Сумма платежа в копейках.")
    purpose: str | None = Field(
        default=None, max_length=210, description="Назначение платежа."
    )
    last_name: str | None = Field(default=None, max_length=160)
    first_name: str | None = Field(default=None, max_length=160)
    middle_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=25)

    def to_payment_request(self) -> PaymentRequest:
        """Превращает тело запроса во внутреннюю схему."""
        return PaymentRequest(
            sum_kopecks=self.sum,
            purpose=self.purpose,
            payer=PayerInfo(
                last_name=self.last_name,
                first_name=self.first_name,
                middle_name=self.middle_name,
                phone=self.phone,
            ),
        )


EMPTY_BODY = QrRequestBody()
"""Тело по умолчанию: запрос без параметров тоже валиден — плательщик
введёт сумму в банковском приложении сам."""


def _requisites_of(organization: Organization) -> PayeeRequisites:
    """Собирает реквизиты получателя из модели организации."""
    return PayeeRequisites(
        name=organization.name,
        personal_acc=organization.personal_acc,
        bank_name=organization.bank_name,
        bic=organization.bic,
        corresp_acc=organization.corresp_acc,
        payee_inn=organization.payee_inn,
        kpp=organization.kpp,
    )


@router.get(
    "/v1/qr",
    summary="Получить QR-код (для шаблонов CRM)",
    description=(
        "Отдаёт PNG с QR-кодом по ГОСТ Р 56042-2014. Адрес этого эндпоинта "
        "вставляется прямо в тег <img> шаблона письма или счёта.\n\n"
        "Ошибки по умолчанию возвращаются картинкой с текстом, а не кодом "
        "ответа: иначе получатель письма увидел бы битую иконку. Код ошибки "
        "дублируется в заголовке X-NoSBP-Error. Параметр on_error=status "
        "переключает поведение на обычные HTTP-коды."
    ),
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def get_qr(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    logo_cache: LogoCacheDep,
    token: Annotated[str, Query(description="Ключ API.")],
    org: Annotated[str | None, Query(description="Алиас организации.")] = None,
    sum: Annotated[
        int | None, Query(ge=1, description="Сумма платежа в копейках.")
    ] = None,
    purpose: Annotated[str | None, Query(max_length=210)] = None,
    last_name: Annotated[str | None, Query(max_length=160)] = None,
    first_name: Annotated[str | None, Query(max_length=160)] = None,
    middle_name: Annotated[str | None, Query(max_length=160)] = None,
    phone: Annotated[str | None, Query(max_length=25)] = None,
    on_error: Annotated[str, Query(pattern="^(image|status)$")] = "image",
) -> Response:
    """Возвращает PNG с QR-кодом для оплаты по реквизитам."""
    payment = PaymentRequest(
        sum_kopecks=sum,
        purpose=purpose,
        payer=PayerInfo(
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            phone=phone,
        ),
    )
    try:
        return await _generate(
            request=request,
            session=session,
            settings=settings,
            logo_cache=logo_cache,
            token=token,
            org=org,
            payment=payment,
        )
    except NosbpError as error:
        if on_error == "status":
            raise
        log.warning(
            "qr_error_rendered_as_image", code=error.code, message=error.message
        )
        return Response(
            content=render_placeholder_png(error.message),
            media_type="image/png",
            headers={ERROR_HEADER: error.code, "Cache-Control": "no-store"},
        )


@router.post(
    "/v1/qr",
    summary="Получить QR-код (для интеграций)",
    description=(
        "То же самое, что GET, но ключ передаётся заголовком Authorization "
        "и не попадает ни в логи прокси, ни в историю браузера. "
        "Ошибки возвращаются обычными кодами ответа и JSON."
    ),
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
async def post_qr(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    logo_cache: LogoCacheDep,
    authorization: Annotated[
        str, Header(description="Ключ API в формате «Bearer <токен>».")
    ],
    body: Annotated[QrRequestBody, Body()] = EMPTY_BODY,
) -> Response:
    """Возвращает PNG с QR-кодом. Ключ — в заголовке Authorization."""
    token = authorization.removeprefix("Bearer ").strip()
    return await _generate(
        request=request,
        session=session,
        settings=settings,
        logo_cache=logo_cache,
        token=token,
        org=body.org,
        payment=body.to_payment_request(),
    )


async def _generate(
    *,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    logo_cache: LogoCacheDep,
    token: str,
    org: str | None,
    payment: PaymentRequest,
) -> Response:
    """Общая часть GET и POST: от токена до готового PNG."""
    account: Account = await authenticate(session, token)
    organization = await resolve_organization(session, account, org)

    # Строка и цвет проверяются до тарификации: списывать деньги за счёт,
    # который потом не удастся нарисовать, нельзя.
    payload = build_gost_payload(_requisites_of(organization), payment)
    color = validate_qr_color(organization.qr_color)

    resolution = await InvoiceService(session, settings).resolve(
        account=account, organization=organization, payment=payment
    )
    await session.commit()

    invoice = resolution.invoice
    etag = f'"{invoice.idempotency_key[:32]}"'

    log.info(
        "qr_generated",
        account=str(account.id),
        organization=organization.alias,
        invoice=invoice.public_token,
        charged=resolution.charged,
        hits=invoice.hit_count,
    )

    # Картинка для одного и того же счёта не меняется, поэтому клиенту
    # незачем скачивать её повторно. Это снимает с сервиса основную часть
    # нагрузки: почтовые прокси кэшируют изображение у себя.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    logo = await logo_cache.get(organization.logo_key)
    png = render_qr_png(
        payload,
        logo=logo,
        color=color,
        scale=settings.qr_scale,
        border=settings.qr_border,
    )

    max_age = settings.invoice_free_period_days * 24 * 3600
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "ETag": etag,
            "Cache-Control": f"public, max-age={max_age}",
            "X-NoSBP-Invoice": invoice.public_token,
            "X-NoSBP-Charged": "1" if resolution.charged else "0",
        },
    )
