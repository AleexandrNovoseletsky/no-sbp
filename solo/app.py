"""Эндпоинт генерации платёжного QR-кода для одной организации.

Контракт совпадает с полным сервисом: тот же путь, те же имена
параметров, те же заголовки. При переключении в шаблонах CRM меняется
только адрес сервера, а при возвращении обратно — меняется назад.

Чего здесь нет по сравнению с полным сервисом: базы данных, учёта
счетов и тарификации, личного кабинета, панели управления, нескольких
организаций и POST-запросов.
"""

import hashlib
import hmac
import logging
import os
from enum import StrEnum
from typing import Annotated, Final

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from nosbp.core.constants import (
    MAX_SUM_KOPECKS,
    NAME_MAX_LENGTH,
    PHONE_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
    SERVICE_NAME,
)
from nosbp.core.errors import InvalidTokenError, NosbpError, OrganizationNotFoundError
from nosbp.payments.gost import build_gost_payload
from nosbp.payments.placeholder import render_placeholder_png
from nosbp.payments.qr import render_qr_png
from nosbp.payments.schemas import PayerInfo, PaymentRequest
from solo.config import ConfigError, Settings, load_settings

QR_PATH: Final[str] = "/v1/qr"

ERROR_HEADER: Final[str] = "X-NoSBP-Error"
"""Машиночитаемый код ошибки, когда в теле ответа лежит картинка."""

ETAG_LENGTH: Final = 32
"""Сколько шестнадцатеричных цифр хэша берётся в ETag.

Половины SHA-256 более чем достаточно, чтобы два разных платежа
не совпали, а заголовок остаётся коротким.
"""

NOT_MODIFIED: Final = 304
NO_STORE: Final[str] = "no-store"

log = logging.getLogger("nosbp-solo")


class InvalidParametersError(NosbpError):
    """Параметры запроса не разобрались.

    Отдельный класс нужен, чтобы ошибка разбора параметров доходила
    до получателя письма тем же способом, что и остальные, — картинкой
    с читаемым текстом, а не битой иконкой.
    """

    code = "invalid_parameters"


class OnError(StrEnum):
    """Как отдавать ошибку."""

    IMAGE = "image"
    """Картинкой с текстом. По умолчанию: адрес стоит в теге <img>
    уже отправленного письма, и код ответа превратился бы в битую иконку."""

    STATUS = "status"
    """Обычным HTTP-кодом и JSON — для тех, кто вызывает API из программы."""


def _authorize(token: str, settings: Settings) -> None:
    """Сверяет ключ доступа.

    Сравнение за постоянное время: по времени ответа на разные ключи
    можно восстановить правильный посимвольно.

    :raises InvalidTokenError: ключ не совпал.
    """
    if not hmac.compare_digest(token, settings.token):
        raise InvalidTokenError("Ключ не найден или отозван.")


def _check_organization(alias: str | None, settings: Settings) -> None:
    """Проверяет, что запрошена та единственная организация, что настроена.

    Параметр остаётся ради совместимости с шаблонами CRM. Молча
    подставлять свои реквизиты вместо запрошенных нельзя: заказчик
    получил бы QR-код на чужой счёт и заметил бы это в лучшем случае
    после оплаты.

    :raises OrganizationNotFoundError: запрошена другая организация.
    """
    if alias is not None and alias != settings.org_alias:
        raise OrganizationNotFoundError(
            f"Организация «{alias}» не найдена. Проверьте параметр org."
        )


def _error_response(error: NosbpError) -> Response:
    """Отдаёт текст ошибки картинкой, а не кодом ответа."""
    log.warning("ошибка отрисована картинкой: %s — %s", error.code, error.message)
    return Response(
        content=render_placeholder_png(error.message),
        media_type="image/png",
        headers={ERROR_HEADER: error.code, "Cache-Control": NO_STORE},
    )


def _wants_image(request: Request) -> bool:
    """Ждёт ли клиент картинку вместо кода ответа."""
    return request.query_params.get("on_error", "") != OnError.STATUS


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собирает приложение.

    :param settings: конфигурация. По умолчанию читается из окружения;
        явная передача используется в тестах.
    """
    try:
        settings = settings or load_settings()
    except ConfigError as error:
        # Понятная строка вместо стека вызовов: эту ошибку читает тот,
        # кто разворачивает сервис, а не тот, кто его писал.
        raise SystemExit(f"Сервис не запущен. {error}") from error

    # Документация не отдаётся: сервис стоит на публичном адресе,
    # а описывать в нём нечего — эндпоинт ровно один.
    app = FastAPI(
        title=f"{SERVICE_NAME} solo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.exception_handler(NosbpError)
    async def handle_domain_error(request: Request, exc: NosbpError) -> Response:
        """Превращает доменную ошибку в JSON — или в картинку с текстом."""
        if request.url.path == QR_PATH and _wants_image(request):
            return _error_response(exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_bad_parameters(
        request: Request, exc: RequestValidationError
    ) -> Response:
        """Отдаёт картинкой и ошибки разбора параметров.

        Без этого обработчика опечатка в сумме — «sum=abc» вместо числа —
        превращалась бы в битую иконку у получателя письма, хотя ровно
        от этого картинка с текстом и защищает.
        """
        if request.url.path == QR_PATH and _wants_image(request):
            return _error_response(
                InvalidParametersError(
                    "Неверные параметры запроса. Проверьте сумму и назначение платежа."
                )
            )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.get("/health", summary="Проверка живости")
    async def health() -> dict[str, str]:
        """Отвечает, пока процесс жив. Используется Docker и мониторингом."""
        return {"status": "ok"}

    @app.get(QR_PATH, response_class=Response)
    async def get_qr(
        request: Request,
        token: Annotated[str, Query(description="Ключ доступа.")],
        org: Annotated[str | None, Query(description="Алиас организации.")] = None,
        # Имя параметра затеняет встроенный sum, но менять его нельзя:
        # оно уже стоит в шаблонах CRM.
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
        current: Settings = request.app.state.settings

        _authorize(token, current)
        _check_organization(org, current)

        payload = build_gost_payload(
            current.requisites,
            PaymentRequest(
                sum_kopecks=sum,
                purpose=purpose,
                payer=PayerInfo(
                    last_name=last_name,
                    first_name=first_name,
                    middle_name=middle_name,
                    phone=phone,
                ),
            ),
        )

        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        etag = f'"{digest[:ETAG_LENGTH]}"'
        cache = f"public, max-age={current.cache_max_age_seconds}"

        # Картинка одного и того же платежа неизменна, поэтому повторная
        # загрузка не нужна. Кэш почтовых прокси снимает с сервиса
        # основную часть обращений.
        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=NOT_MODIFIED,
                headers={"ETag": etag, "Cache-Control": cache},
            )

        log.info("QR: сумма=%s назначение=%s", sum, purpose)
        return Response(
            content=render_qr_png(
                payload,
                logo=current.logo,
                color=current.qr_color,
                scale=current.qr_scale,
                border=current.qr_border,
            ),
            media_type="image/png",
            headers={"ETag": etag, "Cache-Control": cache},
        )

    return app


def _configure_logging() -> None:
    """Настраивает вывод журнала в поток контейнера."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_configure_logging()
