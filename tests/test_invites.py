"""Тесты одноразовых ссылок — низкоуровневая часть механизма.

Ссылки решают три задачи сразу: первый вход в аккаунт, заведённый
оператором, восстановление забытого пароля и подтверждение адреса почты.
Здесь проверяется сам механизм: срок жизни, одноразовость, назначение.
Сценарии целиком лежат в ``test_password_reset`` и
``test_email_confirmation``.
"""

import datetime

import pytest
from sqlalchemy import select

from nosbp.cabinet import invites
from nosbp.cabinet.service import SESSION_COOKIE_NAME
from nosbp.core.config import get_settings
from nosbp.core.errors import ValidationError
from nosbp.core.security import verify_password
from nosbp.db.models import AccountInvite, InvitePurpose
from tests.factories import ADMIN_PREFIX, CABINET_PREFIX

NEW_PASSWORD = "novyj-parol-2026"


# ---------------------------------------------------------------------------
# Выпуск и погашение
# ---------------------------------------------------------------------------


async def test_issue_returns_token_and_stores_only_hash(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    invite = (
        await session.execute(
            select(AccountInvite).where(AccountInvite.account_id == account.id)
        )
    ).scalar_one()

    assert token
    assert token not in invite.token_hash
    assert len(invite.token_hash) == 64


async def test_new_link_cancels_the_previous_one(session, make_account):
    """У аккаунта не должно быть двух действующих ссылок сразу."""
    account = await make_account()
    first = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    await invites.issue(session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72)
    await session.commit()

    with pytest.raises(ValidationError, match="недействительна"):
        await invites.find_account(session, first, purpose=InvitePurpose.PASSWORD)


async def test_link_sets_password(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    await invites.use(session, token, NEW_PASSWORD)
    await session.commit()

    assert account.password_hash is not None
    assert verify_password(account.password_hash, NEW_PASSWORD)


async def test_link_is_single_use(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    await invites.use(session, token, NEW_PASSWORD)
    await session.commit()

    with pytest.raises(ValidationError, match="недействительна"):
        await invites.use(session, token, "drugoj-parol-2026")


async def test_expired_link_is_refused(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    invite = (
        await session.execute(
            select(AccountInvite).where(AccountInvite.account_id == account.id)
        )
    ).scalar_one()
    invite.expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )
    await session.commit()

    with pytest.raises(ValidationError, match="недействительна"):
        await invites.find_account(session, token, purpose=InvitePurpose.PASSWORD)


async def test_unknown_link_is_refused(session):
    with pytest.raises(ValidationError, match="недействительна"):
        await invites.find_account(
            session, "выдуманный токен", purpose=InvitePurpose.PASSWORD
        )


async def test_weak_password_is_refused(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    with pytest.raises(ValidationError, match="Пароль должен"):
        await invites.use(session, token, "короткий")


async def test_link_for_disabled_account_is_refused(session, make_account):
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    account.is_active = False
    await session.commit()

    with pytest.raises(ValidationError, match="недоступна"):
        await invites.find_account(session, token, purpose=InvitePurpose.PASSWORD)


def test_url_is_built_from_settings():
    url = invites.build_url(
        "https://example.com/", "/cabinet", "TOKEN", purpose=InvitePurpose.PASSWORD
    )
    assert url == "https://example.com/cabinet/password/TOKEN"


def test_confirmation_url_leads_to_its_own_page():
    url = invites.build_url(
        "https://example.com", "/cabinet", "TOKEN", purpose=InvitePurpose.EMAIL
    )
    assert url == "https://example.com/cabinet/confirm/TOKEN"


async def test_link_cannot_be_used_for_another_purpose(session, make_account):
    """Письмо о подтверждении адреса не должно открывать смену пароля."""
    account = await make_account()
    token = await invites.issue(
        session, account, purpose=InvitePurpose.EMAIL, ttl_hours=48
    )
    await session.commit()

    with pytest.raises(ValidationError, match="недействительна"):
        await invites.use(session, token, NEW_PASSWORD)


async def test_link_of_one_purpose_does_not_cancel_the_other(session, make_account):
    account = await make_account()
    confirmation = await invites.issue(
        session, account, purpose=InvitePurpose.EMAIL, ttl_hours=48
    )
    await invites.issue(session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=1)
    await session.commit()

    assert await invites.confirm(session, confirmation) is account


# ---------------------------------------------------------------------------
# Сквозной сценарий: оператор завёл аккаунт, заказчик вошёл
# ---------------------------------------------------------------------------


async def test_admin_created_account_cannot_sign_in_without_link(cabinet, make_account):
    """Ровно та ситуация, из-за которой ссылки и появились."""
    account = await make_account()

    signed = await cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": account.email, "password": "любой-пароль-12"},
    )
    assert signed.status_code == 200
    assert "Неверная почта или пароль" in signed.text

    registered = await cabinet.post(
        f"{CABINET_PREFIX}/register",
        data={
            "email": account.email,
            "display_name": "Попытка",
            "password": NEW_PASSWORD,
            "password_repeat": NEW_PASSWORD,
        },
    )
    assert registered.status_code == 200
    assert "уже заведён аккаунт" in registered.text
    assert "ссылку" in registered.text


async def test_full_flow_from_panel_to_cabinet(
    logged_in, cabinet, session, make_account
):
    """Оператор выдаёт ссылку, заказчик по ней задаёт пароль и входит."""
    panel, _, csrf = logged_in
    account = await make_account()

    issued = await panel.post(
        f"{ADMIN_PREFIX}/accounts/{account.id}/invite", data={"csrf_token": csrf}
    )
    assert issued.status_code == 303

    location = issued.headers["location"]
    assert "invite=" in location

    from urllib.parse import parse_qs, unquote, urlparse

    invite_url = parse_qs(urlparse(location).query)["invite"][0]
    token = unquote(invite_url).rsplit("/", 1)[1]

    form = await cabinet.get(f"{CABINET_PREFIX}/password/{token}")
    assert form.status_code == 200
    assert account.email in form.text

    saved = await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )
    assert saved.status_code == 303
    assert cabinet.cookies.get(SESSION_COOKIE_NAME)

    dashboard = await cabinet.get(f"{CABINET_PREFIX}/")
    assert dashboard.status_code == 200
    assert account.display_name in dashboard.text


async def test_link_page_explains_when_token_is_bad(cabinet):
    response = await cabinet.get(f"{CABINET_PREFIX}/password/выдуманный")
    assert response.status_code == 200
    assert "недействительна" in response.text


async def test_forgot_page_offers_a_form(cabinet):
    response = await cabinet.get(f"{CABINET_PREFIX}/forgot")
    assert response.status_code == 200
    assert "Восстановление пароля" in response.text
    assert 'name="email"' in response.text


async def test_mismatched_passwords_on_link(cabinet, session, make_account):
    account = await make_account()
    token = await invites.issue(
        session,
        account,
        purpose=InvitePurpose.PASSWORD,
        ttl_hours=get_settings().cabinet_invite_ttl_hours,
    )
    await session.commit()

    response = await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": "другой-пароль-12"},
    )
    assert response.status_code == 200
    assert "не совпадают" in response.text


async def test_panel_shows_accounts_without_access(logged_in, make_account):
    """Оператор должен видеть, кто ещё не может войти в кабинет."""
    panel, _, _ = logged_in
    await make_account(email="bez-parolya@example.com")

    response = await panel.get(f"{ADMIN_PREFIX}/")
    assert "нет входа" in response.text
