"""Ключи API в панели управления."""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse

from nosbp.admin import crud
from nosbp.admin.dependencies import CsrfProtected
from nosbp.db.base import utcnow
from nosbp.db.models import ApiToken
from nosbp.payments.dependencies import AppSettings, DbSession
from nosbp.web.responses import redirect

router = APIRouter()
log = structlog.get_logger()


@router.post("/accounts/{account_id}/tokens")
async def issue(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    account_id: uuid.UUID,
    label: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Выпускает ключ и показывает его один раз."""
    account = await crud.get_account(db, account_id)
    token = await crud.issue_token(db, account, label=label)
    await db.commit()

    log.info("admin_token_issued", admin=viewer.email, account=account.email)
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account_id}",
        token=token,
        ok="Ключ выпущен. Он показывается один раз — скопируйте его сейчас.",
    )


@router.post("/accounts/{account_id}/tokens/{token_id}/revoke")
async def revoke(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    account_id: uuid.UUID,
    token_id: uuid.UUID,
) -> RedirectResponse:
    """Отзывает ключ."""
    api_token = await db.get(ApiToken, token_id)
    if api_token is not None and api_token.revoked_at is None:
        api_token.revoked_at = utcnow()
        await db.commit()
        log.warning("admin_token_revoked", admin=viewer.email, prefix=api_token.prefix)
    return redirect(
        settings.admin_prefix, f"/accounts/{account_id}", ok="Ключ отозван."
    )
