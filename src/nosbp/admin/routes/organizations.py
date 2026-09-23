"""Организации-получатели платежа в панели управления."""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin import crud
from nosbp.admin.dependencies import CsrfProtected, CurrentAdmin, Templates
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.errors import NosbpError, OrganizationNotFoundError
from nosbp.db.models import Organization
from nosbp.payments.dependencies import AppSettings, DbSession, LogoCacheDep
from nosbp.web import organizations as forms
from nosbp.web.responses import error_text, page, redirect, reload_after_rollback

router = APIRouter()
log = structlog.get_logger()

Field = Annotated[str, Form()]


async def _get(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    """Находит организацию.

    :raises OrganizationNotFoundError: организации нет.
    """
    organization = await db.get(Organization, organization_id)
    if organization is None:
        raise OrganizationNotFoundError("Организация не найдена.")
    return organization


@router.get("/accounts/{account_id}/organizations/new")
async def new_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAdmin,
    account_id: uuid.UUID,
) -> Response:
    """Форма добавления организации."""
    return page(
        templates,
        request,
        "organization_form.html",
        settings,
        viewer=viewer,
        account=await crud.get_account(db, account_id),
        organization=None,
        form=forms.values_of(None, settings),
    )


@router.post("/accounts/{account_id}/organizations/new")
async def create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    logo_cache: LogoCacheDep,
    account_id: uuid.UUID,
    alias: Field,
    name: Field,
    personal_acc: Field,
    bank_name: Field,
    bic: Field,
    corresp_acc: Field,
    payee_inn: Field,
    kpp: Field = "",
    qr_color: Field = DEFAULT_QR_COLOR,
    fee_percent: Field = "",
    average_check: Field = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Добавляет организацию."""
    account = await crud.get_account(db, account_id)
    submitted = forms.submitted_values(
        alias=alias,
        name=name,
        personal_acc=personal_acc,
        bank_name=bank_name,
        bic=bic,
        corresp_acc=corresp_acc,
        payee_inn=payee_inn,
        kpp=kpp,
        qr_color=qr_color,
        fee_percent=fee_percent,
        average_check=average_check,
        is_default=is_default,
    )
    try:
        data = forms.to_requisites(submitted, settings.default_acquiring_fee_bps)
        organization = await crud.create_organization(db, account, data)
        await forms.attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await reload_after_rollback(db, account)
        return page(
            templates,
            request,
            "organization_form.html",
            settings,
            viewer=viewer,
            err=error_text(error),
            account=account,
            organization=None,
            form=submitted,
        )

    log.info(
        "admin_organization_created",
        admin=viewer.email,
        organization=organization.alias,
    )
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account_id}",
        ok=f"Организация «{organization.alias}» добавлена.",
    )


@router.get("/organizations/{organization_id}")
async def edit_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAdmin,
    organization_id: uuid.UUID,
) -> Response:
    """Форма изменения организации."""
    organization = await _get(db, organization_id)
    return page(
        templates,
        request,
        "organization_form.html",
        settings,
        viewer=viewer,
        account=await crud.get_account(db, organization.account_id),
        organization=organization,
        form=forms.values_of(organization, settings),
    )


@router.post("/organizations/{organization_id}/edit")
async def update(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    logo_cache: LogoCacheDep,
    organization_id: uuid.UUID,
    alias: Field,
    name: Field,
    personal_acc: Field,
    bank_name: Field,
    bic: Field,
    corresp_acc: Field,
    payee_inn: Field,
    kpp: Field = "",
    qr_color: Field = DEFAULT_QR_COLOR,
    fee_percent: Field = "",
    average_check: Field = "",
    is_default: Annotated[str | None, Form()] = None,
    logo: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Сохраняет организацию."""
    organization = await _get(db, organization_id)
    account = await crud.get_account(db, organization.account_id)
    submitted = forms.submitted_values(
        alias=alias,
        name=name,
        personal_acc=personal_acc,
        bank_name=bank_name,
        bic=bic,
        corresp_acc=corresp_acc,
        payee_inn=payee_inn,
        kpp=kpp,
        qr_color=qr_color,
        fee_percent=fee_percent,
        average_check=average_check,
        is_default=is_default,
    )
    try:
        data = forms.to_requisites(submitted, settings.default_acquiring_fee_bps)
        await crud.update_organization(db, organization, data)
        await forms.attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await reload_after_rollback(db, account, organization)
        return page(
            templates,
            request,
            "organization_form.html",
            settings,
            viewer=viewer,
            err=error_text(error),
            account=account,
            organization=organization,
            form=submitted,
        )

    log.info(
        "admin_organization_updated",
        admin=viewer.email,
        organization=organization.alias,
    )
    return redirect(
        settings.admin_prefix, f"/accounts/{account.id}", ok="Организация сохранена."
    )


@router.post("/organizations/{organization_id}/delete")
async def delete(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    organization_id: uuid.UUID,
) -> RedirectResponse:
    """Удаляет организацию вместе с её счетами."""
    organization = await _get(db, organization_id)
    account_id = organization.account_id
    alias = organization.alias

    await crud.delete_organization(db, organization)
    await db.commit()

    log.warning("admin_organization_deleted", admin=viewer.email, organization=alias)
    return redirect(
        settings.admin_prefix,
        f"/accounts/{account_id}",
        ok=f"Организация «{alias}» удалена.",
    )
