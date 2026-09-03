"""HTTP-эндпоинты генерации платёжного QR-кода."""

import datetime
from enum import StrEnum
from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Body, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.constants import (
    MAX_SUM_KOPECKS,
    NAME_MAX_LENGTH,
    PHONE_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
)
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
from nosbp.storage.logo_cache import LogoCache

router = APIRouter(tags=["qr"])
log = structlog.get_logger()

QR_PATH: Final[str] = "/v1/qr"

ERROR_HEADER: Final[str] = "X-NoSBP-Error"
"""Машиночитаемый код ошибки, когда в теле ответа лежит картинка-заглушка."""

INVOICE_HEADER: Final[str] = "X-NoSBP-Invoice"
CHARGED_HEADER: Final[str] = "X-NoSBP-Charged"

BEARER_PREFIX: Final[str] = "Bearer "

ETAG_LENGTH: Final = 32
"""Сколько символов ключа идемпотентности берётся в ETag.

Половины SHA-256 более чем достаточно, чтобы два разных счёта не совпали,
а заголовок остаётся коротким.
"""

NOT_MODIFIED: Final = 304


class OnError(StrEnum):
    """Как отдавать ошибку на GET-запросе."""

    IMAGE = "image"
    """Картинкой с текстом. По умолчанию: адрес стоит в теге <img>
    уже отправленного письма, и код ответа превратился бы в битую иконку."""

    STATUS = "status"
    """Обычным HTTP-кодом и JSON — для тех, кто вызывает API из программы."""


class QrRequestBody(BaseModel):
    """Тело POST-запроса на генерацию QR-кода."""

    model_config = ConfigDict(frozen=True)

    org: str | None = Field(
        default=None, description="Алиас организации-получателя платежа."
    )
    sum: int | None = Field(
        default=None,
        ge=1,
        le=MAX_SUM_KOPECKS,
        description="Сумма платежа в копейках.",
    )
    purpose: str | None = Field(
        default=None, max_length=PURPOSE_MAX_LENGTH, description="Назначение платежа."
    )
    last_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    first_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    middle_name: str | None = Field(default=None, max_length=NAME_MAX_LENGTH)
    phone: str | None = Field(default=None, max_length=PHONE_MAX_LENGTH)

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


EMPTY_BODY: Final = QrRequestBody()
"""Тело по умолчанию: запрос без параметров тоже валиден — плательщик
введёт сумму в банковском приложении сам. Модель заморожена, поэтому
общий экземпляр безопасен."""


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
    QR_PATH,
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
    # Имя параметра затеняет встроенный sum, но менять его нельзя:
    # оно уже стоит в шаблонах CRM у работающих заказчиков.
    sum: Annotated[
        int | None,
        Query(ge=1, le=MAX_SUM_KOPECKS, description="Сумма платежа в копейках."),
    ] = None,
    purpose: Annotated[str | None, Query(max_length=PURPOSE_MAX_LENGTH)] = None,
    last_name: Annotated[str | None, Query(max_length=NAME_MAX_LENGTH)] = None,
    first_name: Annotated[str | None, Query(max_length=NAME_MAX_LENGTH)] = None,
    middle_name: Annotated[str | None, Query(max_length=NAME_MAX_LENGTH)] = None,
    phone: Annotated[str | None, Query(max_length=PHONE_MAX_LENGTH)] = None,
    on_error: Annotated[
        OnError, Query(description="Как отдавать ошибку.")
    ] = OnError.IMAGE,
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
        if on_error is OnError.STATUS:
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
    QR_PATH,
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
    return await _generate(
        request=request,
        session=session,
        settings=settings,
        logo_cache=logo_cache,
        token=authorization.removeprefix(BEARER_PREFIX).strip(),
        org=body.org,
        payment=body.to_payment_request(),
    )


async def _generate(
    *,
    request: Request,
    session: AsyncSession,
    settings: Settings,
    logo_cache: LogoCache,
    token: str,
    org: str | None,
    payment: PaymentRequest,
) -> Response:
    """Общая часть GET и POST: от токена до готового PNG."""
    account: Account = await authenticate(session, token, settings=settings)
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
    etag = f'"{invoice.idempotency_key[:ETAG_LENGTH]}"'

    log.info(
        "qr_generated",
        account=str(account.id),
        organization=organization.alias,
        invoice=invoice.public_token,
        charged=resolution.charged,
        hits=invoice.hit_count,
    )

    # Изображение одного и того же счёта неизменно, поэтому повторная
    # загрузка не требуется. Кэширование на стороне почтовых прокси снимает
    # с сервиса основную часть нагрузки.
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=NOT_MODIFIED, headers={"ETag": etag})

    logo = await logo_cache.get(organization.logo_key)
    png = render_qr_png(
        payload,
        logo=logo,
        color=color,
        scale=settings.qr_scale,
        border=settings.qr_border,
    )

    # Кэшируем ровно на срок бесплатного периода: дольше нельзя — после
    # него тот же адрес порождает новый тарифицируемый счёт.
    max_age = int(
        datetime.timedelta(days=settings.invoice_free_period_days).total_seconds()
    )
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "ETag": etag,
            "Cache-Control": f"public, max-age={max_age}",
            INVOICE_HEADER: invoice.public_token,
            CHARGED_HEADER: "1" if resolution.charged else "0",
        },
    )
