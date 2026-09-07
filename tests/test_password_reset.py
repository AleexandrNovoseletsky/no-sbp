"""Тесты самостоятельного восстановления пароля по почте.

Проверяют весь путь: форма — письмо — ссылка — новый пароль — вход,
а также то, что формой нельзя проверить, зарегистрирован ли адрес.
"""

import datetime

from sqlalchemy import select

from nosbp.cabinet import invites
from nosbp.cabinet.service import SESSION_COOKIE_NAME
from nosbp.core.config import get_settings
from nosbp.db.models import Account, AccountInvite, InvitePurpose
from tests.factories import CABINET_PREFIX

NEW_PASSWORD = "sovsem-novyj-parol-77"
LINK_FRAGMENT = "/password/"


async def _request_reset(cabinet, email: str):
    """Отправляет форму восстановления."""
    return await cabinet.post(f"{CABINET_PREFIX}/forgot", data={"email": email})


# ---------------------------------------------------------------------------
# Сквозной сценарий
# ---------------------------------------------------------------------------


async def test_forgotten_password_is_restored_end_to_end(
    cabinet, mailbox, make_client_account
):
    account, old_password = await make_client_account()

    requested = await _request_reset(cabinet, account.email)
    assert requested.status_code == 200
    assert "Если этот адрес зарегистрирован" in requested.text

    letter = mailbox.last
    assert letter.to == account.email
    assert "Восстановление пароля" in letter.subject

    token = letter.token_after(LINK_FRAGMENT)
    form = await cabinet.get(f"{CABINET_PREFIX}/password/{token}")
    assert form.status_code == 200
    assert account.email in form.text

    saved = await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )
    assert saved.status_code == 303
    assert cabinet.cookies.get(SESSION_COOKIE_NAME)

    # Старый пароль больше не работает, новый работает.
    cabinet.cookies.clear()
    refused = await cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": account.email, "password": old_password},
    )
    assert refused.status_code == 200

    accepted = await cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": account.email, "password": NEW_PASSWORD},
    )
    assert accepted.status_code == 303


async def test_password_never_set_is_restored_the_same_way(
    cabinet, mailbox, make_account
):
    """Аккаунт, заведённый оператором: пароля нет, восстановление то же."""
    account = await make_account(email="bez-parolya@example.com")

    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)

    saved = await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )
    assert saved.status_code == 303
    assert cabinet.cookies.get(SESSION_COOKIE_NAME)


async def test_restoring_confirms_the_address(
    cabinet, mailbox, session, make_client_account
):
    """Переход по ссылке из письма доказывает доступ к ящику."""
    account, _ = await make_client_account()
    assert account.email_confirmed_at is None

    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)
    await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )

    await session.refresh(account)
    assert account.email_confirmed_at is not None


async def test_other_sessions_are_closed_after_reset(
    cabinet, mailbox, signed_in, make_client_account
):
    """Если пароль восстанавливали из-за угона, чужой кабинет закроется."""
    _, account, _ = signed_in
    assert (await cabinet.get(f"{CABINET_PREFIX}/")).status_code == 200

    stolen = cabinet.cookies.get(SESSION_COOKIE_NAME)
    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)

    cabinet.cookies.clear()
    await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )

    cabinet.cookies.clear()
    cabinet.cookies.set(SESSION_COOKIE_NAME, stolen, domain="cabinet.test")
    assert (await cabinet.get(f"{CABINET_PREFIX}/")).status_code == 303


async def test_changed_password_is_reported_to_the_owner(
    cabinet, mailbox, make_client_account
):
    account, _ = await make_client_account()

    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)
    await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )

    assert mailbox.last.subject == "Пароль в NoSBP изменён"
    assert mailbox.last.to == account.email


async def test_first_password_is_not_reported_as_a_change(
    cabinet, mailbox, make_account
):
    """Аккаунту без пароля письмо «пароль изменён» ни к чему."""
    account = await make_account()

    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)
    await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )

    assert [letter.subject for letter in mailbox.sent] == [
        "Восстановление пароля в NoSBP"
    ]


# ---------------------------------------------------------------------------
# Что форма не должна выдавать
# ---------------------------------------------------------------------------


async def test_unknown_address_gets_the_same_answer(cabinet, mailbox):
    """Иначе формой можно собрать список зарегистрированных адресов."""
    response = await _request_reset(cabinet, "nikogo-net@example.com")

    assert response.status_code == 200
    assert "Если этот адрес зарегистрирован" in response.text
    assert mailbox.sent == []


async def test_disabled_account_gets_no_letter(cabinet, mailbox, make_client_account):
    account, _ = await make_client_account(is_active=False)

    response = await _request_reset(cabinet, account.email)

    assert "Если этот адрес зарегистрирован" in response.text
    assert mailbox.sent == []


async def test_repeated_requests_are_limited(cabinet, mailbox, make_client_account):
    """Чужой почтовый ящик нельзя завалить письмами через форму."""
    account, _ = await make_client_account()
    limit = get_settings().password_reset_max_per_hour

    for _ in range(limit + 2):
        response = await _request_reset(cabinet, account.email)
        assert "Если этот адрес зарегистрирован" in response.text

    assert len(mailbox.sent) == limit


async def test_limit_counts_only_the_recent_hour(
    cabinet, mailbox, session, make_client_account
):
    account, _ = await make_client_account()
    limit = get_settings().password_reset_max_per_hour

    for _ in range(limit):
        await _request_reset(cabinet, account.email)
    mailbox.clear()

    # Старые ссылки отодвигаются в прошлое — лимит должен освободиться.
    long_ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2)
    invites_of_account = await session.execute(
        select(AccountInvite).where(AccountInvite.account_id == account.id)
    )
    for invite in invites_of_account.scalars():
        invite.created_at = long_ago
    await session.commit()

    await _request_reset(cabinet, account.email)
    assert len(mailbox.sent) == 1


# ---------------------------------------------------------------------------
# Сама ссылка
# ---------------------------------------------------------------------------


async def test_new_request_cancels_the_previous_link(
    cabinet, mailbox, make_client_account
):
    account, _ = await make_client_account()

    await _request_reset(cabinet, account.email)
    first = mailbox.last.token_after(LINK_FRAGMENT)

    await _request_reset(cabinet, account.email)
    second = mailbox.last.token_after(LINK_FRAGMENT)
    assert first != second

    stale = await cabinet.get(f"{CABINET_PREFIX}/password/{first}")
    assert "недействительна" in stale.text

    fresh = await cabinet.get(f"{CABINET_PREFIX}/password/{second}")
    assert fresh.status_code == 200


async def test_self_service_link_lives_shorter_than_the_operator_one(
    cabinet, mailbox, session, make_client_account
):
    """Ссылку из формы может запросить кто угодно, зная адрес."""
    account, _ = await make_client_account()
    settings = get_settings()

    await _request_reset(cabinet, account.email)

    invite = (
        await session.execute(
            select(AccountInvite).where(
                AccountInvite.account_id == account.id,
                AccountInvite.purpose == InvitePurpose.PASSWORD,
            )
        )
    ).scalar_one()
    lifetime = invite.expires_at - invite.created_at

    assert lifetime <= datetime.timedelta(hours=settings.password_reset_ttl_hours)
    assert settings.password_reset_ttl_hours < settings.cabinet_invite_ttl_hours


async def test_link_is_dead_after_use(cabinet, mailbox, make_client_account):
    account, _ = await make_client_account()

    await _request_reset(cabinet, account.email)
    token = mailbox.last.token_after(LINK_FRAGMENT)

    await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": NEW_PASSWORD, "password_repeat": NEW_PASSWORD},
    )
    cabinet.cookies.clear()

    reused = await cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={
            "password": "ещё-один-пароль-99",
            "password_repeat": "ещё-один-пароль-99",
        },
    )
    assert "недействительна" in reused.text


async def test_letter_is_addressed_to_the_stored_case_of_the_address(
    cabinet, mailbox, session
):
    """Адрес в базе приведён к нижнему регистру — письмо уходит туда же."""
    session.add(
        Account(email="klient@example.com", display_name="ООО Регистр", is_active=True)
    )
    await session.commit()

    await _request_reset(cabinet, "  KLIENT@Example.COM ")

    assert mailbox.last.to == "klient@example.com"


async def test_reset_does_not_touch_a_link_for_another_account(
    cabinet, mailbox, session, make_client_account
):
    first, _ = await make_client_account()
    second, _ = await make_client_account()

    other_token = await invites.issue(
        session, second, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()

    await _request_reset(cabinet, first.email)

    still_valid = await cabinet.get(f"{CABINET_PREFIX}/password/{other_token}")
    assert still_valid.status_code == 200
    assert second.email in still_valid.text
