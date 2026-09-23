"""Ключи API в личном кабинете."""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from nosbp.admin import crud
from nosbp.cabinet.dependencies import CsrfProtected
from nosbp.db.base import utcnow
from nosbp.db.models import ApiToken
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import redirect

router = APIRouter()
log = structlog.get_logger()


@router.post("/tokens")
async def issue(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    label: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Выпускает ключ и показывает его один раз."""
    token = await crud.issue_token(db, viewer.account, label=label)
    await db.commit()

    log.info("cabinet_token_issued", account=viewer.email)
    return redirect(
        settings.cabinet_prefix,
        "/",
        token=token,
        ok="Ключ выпущен. Он показывается один раз — скопируйте его сейчас.",
    )


@router.post("/tokens/{token_id}/revoke")
async def revoke(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    token_id: uuid.UUID,
) -> RedirectResponse:
    """Отзывает ключ.

    Фильтр по владельцу обязателен: без него можно было бы отозвать чужой
    ключ, подставив его идентификатор.
    """
    result = await db.execute(
        select(ApiToken).where(
            ApiToken.id == token_id,
            ApiToken.account_id == viewer.account.id,
        )
    )
    api_token = result.scalar_one_or_none()
    if api_token is None:
        return redirect(settings.cabinet_prefix, "/", err="Ключ не найден.")

    if api_token.revoked_at is None:
        api_token.revoked_at = utcnow()
        await db.commit()
        log.warning(
            "cabinet_token_revoked", account=viewer.email, prefix=api_token.prefix
        )
    return redirect(settings.cabinet_prefix, "/", ok="Ключ отозван.")
