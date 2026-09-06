"""Организации-получатели платежа в личном кабинете."""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.admin import crud
from nosbp.cabinet.dependencies import CsrfProtected, CurrentAccount, Templates
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.errors import NosbpError, OrganizationNotFoundError
from nosbp.db.models import Organization
from nosbp.payments.dependencies import AppSettings, DbSession, LogoCacheDep
from nosbp.web import organizations as forms
from nosbp.web.responses import error_text, page, redirect, reload_after_rollback

router = APIRouter()
log = structlog.get_logger()

Field = Annotated[str, Form()]


async def _own(
    db: AsyncSession, account_id: uuid.UUID, organization_id: uuid.UUID
) -> Organization:
    """Находит организацию, принадлежащую этому заказчику.

    Фильтр по владельцу обязателен: без него подстановка чужого
    идентификатора в адрес открыла бы доступ к чужим реквизитам.

    :raises OrganizationNotFoundError: организации нет либо она принадлежит
        другому заказчику. Ответ в обоих случаях одинаков.
    """
    result = await db.execute(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.account_id == account_id,
        )
    )
    organization = result.scalar_one_or_none()
    if organization is None:
        raise OrganizationNotFoundError("Организация не найдена.")
    return organization


@router.get("/organizations/new")
async def new_form(
    request: Request,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAccount,
) -> Response:
    """Форма добавления организации."""
    return page(
        templates,
        request,
        "organization_form.html",
        settings,
        viewer=viewer,
        organization=None,
        form=forms.values_of(None, settings),
    )


@router.post("/organizations/new")
async def create(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CsrfProtected,
    logo_cache: LogoCacheDep,
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
        organization = await crud.create_organization(db, viewer.account, data)
        await forms.attach_logo(organization, logo, settings, logo_cache)
        await db.commit()
    except (NosbpError, ValueError) as error:
        await db.rollback()
        await reload_after_rollback(db, viewer.account)
        return page(
            templates,
            request,
            "organization_form.html",
            settings,
            viewer=viewer,
            err=error_text(error),
            organization=None,
            form=submitted,
        )

    log.info(
        "cabinet_organization_created",
        account=viewer.email,
        organization=organization.alias,
    )
    return redirect(
        settings.cabinet_prefix,
        "/",
        ok=f"Организация «{organization.alias}» добавлена.",
    )


@router.get("/organizations/{organization_id}")
async def edit_form(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    templates: Templates,
    viewer: CurrentAccount,
    organization_id: uuid.UUID,
) -> Response:
    """Форма изменения организации."""
    organization = await _own(db, viewer.account.id, organization_id)
    return page(
        templates,
        request,
        "organization_form.html",
        settings,
        viewer=viewer,
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
    organization = await _own(db, viewer.account.id, organization_id)
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
        await reload_after_rollback(db, viewer.account, organization)
        return page(
            templates,
            request,
            "organization_form.html",
            settings,
            viewer=viewer,
            err=error_text(error),
            organization=organization,
            form=submitted,
        )

    log.info(
        "cabinet_organization_updated",
        account=viewer.email,
        organization=organization.alias,
    )
    return redirect(settings.cabinet_prefix, "/", ok="Организация сохранена.")


@router.post("/organizations/{organization_id}/delete")
async def delete(
    db: DbSession,
    settings: AppSettings,
    viewer: CsrfProtected,
    organization_id: uuid.UUID,
) -> RedirectResponse:
    """Удаляет организацию вместе с её счетами."""
    organization = await _own(db, viewer.account.id, organization_id)
    alias = organization.alias

    await crud.delete_organization(db, organization)
    await db.commit()

    log.warning(
        "cabinet_organization_deleted", account=viewer.email, organization=alias
    )
    return redirect(settings.cabinet_prefix, "/", ok=f"Организация «{alias}» удалена.")
