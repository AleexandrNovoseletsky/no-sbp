"""Тесты подтверждения адреса почты.

Подтверждение нужно ради восстановления пароля: если адрес указан
с опечаткой, письмо со ссылкой уйдёт в пустоту, и доступ вернуть будет
некуда.
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from nosbp.cabinet.service import SESSION_COOKIE_NAME
from nosbp.core.config import get_settings
from nosbp.db.models import Account, AccountInvite, InvitePurpose
from nosbp.mail.service import Mailer
from nosbp.main import create_app, prepare_state
from tests.factories import CABINET_PREFIX

PASSWORD = "registraciya-42-secret"
LINK_FRAGMENT = "/confirm/"

REGISTRATION = {
    "email": "novyj@example.com",
    "display_name": "ООО Новичок",
    "password": PASSWORD,
    "password_repeat": PASSWORD,
}


async def _register(cabinet, **overrides):
    """Регистрирует заказчика через форму."""
    return await cabinet.post(
        f"{CABINET_PREFIX}/register", data={**REGISTRATION, **overrides}
    )


# ---------------------------------------------------------------------------
# Обычный режим: подтверждение просим, но вход не запираем
# ---------------------------------------------------------------------------


async def test_registration_sends_a_confirmation_letter(cabinet, mailbox):
    response = await _register(cabinet)

    assert response.status_code == 303
    assert mailbox.last.to == REGISTRATION["email"]
    assert "Подтверждение адреса" in mailbox.last.subject


async def test_dashboard_asks_to_confirm_until_it_is_done(cabinet, mailbox):
    await _register(cabinet)

    before = await cabinet.get(f"{CABINET_PREFIX}/")
    assert "не подтверждён" in before.text

    token = mailbox.last.token_after(LINK_FRAGMENT)
    confirmed = await cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")
    assert confirmed.status_code == 303

    after = await cabinet.get(f"{CABINET_PREFIX}/")
    assert "не подтверждён" not in after.text


async def test_confirmation_is_recorded(cabinet, mailbox, session):
    await _register(cabinet)
    token = mailbox.last.token_after(LINK_FRAGMENT)
    await cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")

    account = (
        await session.execute(
            select(Account).where(Account.email == REGISTRATION["email"])
        )
    ).scalar_one()
    assert account.email_confirmed_at is not None


async def test_confirmation_link_is_single_use(cabinet, mailbox):
    await _register(cabinet)
    token = mailbox.last.token_after(LINK_FRAGMENT)

    assert (await cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")).status_code == 303

    reused = await cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")
    assert reused.status_code == 200
    assert "недействительна" in reused.text


async def test_broken_confirmation_link_is_refused(cabinet):
    response = await cabinet.get(f"{CABINET_PREFIX}/confirm/выдуманный")
    assert response.status_code == 200
    assert "недействительна" in response.text


async def test_letter_can_be_sent_again(cabinet, mailbox, signed_in):
    """Первое письмо могло потеряться — кнопка в кабинете шлёт новое."""
    _, account, csrf = signed_in
    mailbox.clear()

    response = await cabinet.post(
        f"{CABINET_PREFIX}/confirm", data={"csrf_token": csrf}
    )

    assert response.status_code == 303
    assert mailbox.last.to == account.email
    assert "Подтверждение адреса" in mailbox.last.subject


async def test_repeat_does_nothing_for_a_confirmed_address(
    cabinet, mailbox, signed_in, session
):
    _, _account, csrf = signed_in
    await cabinet.post(f"{CABINET_PREFIX}/confirm", data={"csrf_token": csrf})
    token = mailbox.last.token_after(LINK_FRAGMENT)
    await cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")
    mailbox.clear()

    # Кука сессии осталась той же — заказчик по-прежнему в кабинете.
    response = await cabinet.post(
        f"{CABINET_PREFIX}/confirm", data={"csrf_token": csrf}
    )

    assert response.status_code == 303
    assert mailbox.sent == []


async def test_repeat_needs_the_form_token(cabinet, signed_in):
    """Чужая страница не должна слать письма от имени открытой сессии."""
    response = await cabinet.post(f"{CABINET_PREFIX}/confirm", data={})
    assert response.status_code == 303
    assert "login" in response.headers["location"]


async def test_confirmation_link_does_not_set_a_password(cabinet, mailbox):
    """Ссылка одного назначения не должна открывать чужую форму."""
    await _register(cabinet)
    token = mailbox.last.token_after(LINK_FRAGMENT)

    response = await cabinet.get(f"{CABINET_PREFIX}/password/{token}")
    assert "недействительна" in response.text


# ---------------------------------------------------------------------------
# Строгий режим: без подтверждения вход закрыт
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def strict_cabinet(storage, mailbox):
    """Кабинет, требующий подтверждения адреса перед входом."""
    settings = get_settings().model_copy(
        update={"cabinet_require_email_confirmation": True}
    )
    app = create_app(settings)
    app.state.storage = storage
    prepare_state(app, settings)
    app.state.mailer = Mailer(mailbox, base_url=settings.public_base_url)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://cabinet.test",
        follow_redirects=False,
    ) as http:
        yield http


async def test_strict_mode_does_not_sign_in_right_after_registration(
    strict_cabinet, mailbox
):
    response = await _register(strict_cabinet)

    assert response.status_code == 303
    assert "login" in response.headers["location"]
    assert strict_cabinet.cookies.get(SESSION_COOKIE_NAME) is None
    assert mailbox.sent


async def test_strict_mode_refuses_login_until_confirmed(strict_cabinet, mailbox):
    await _register(strict_cabinet)

    refused = await strict_cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": REGISTRATION["email"], "password": PASSWORD},
    )
    assert refused.status_code == 200
    assert "не подтверждён" in refused.text

    token = mailbox.last.token_after(LINK_FRAGMENT)
    await strict_cabinet.get(f"{CABINET_PREFIX}/confirm/{token}")

    accepted = await strict_cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": REGISTRATION["email"], "password": PASSWORD},
    )
    assert accepted.status_code == 303


async def test_strict_mode_still_hides_wrong_password(strict_cabinet):
    """Состояние аккаунта не сообщается тому, кто не знает пароля."""
    await _register(strict_cabinet)

    response = await strict_cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": REGISTRATION["email"], "password": "неверный-пароль-12"},
    )
    assert "Неверная почта или пароль" in response.text


async def test_password_link_confirms_the_address_in_strict_mode(
    strict_cabinet, mailbox, session, make_account
):
    """Заказчик оператора не должен застрять между двумя проверками."""
    account = await make_account(email="ot-operatora@example.com")
    token = await _issue_password_link(session, account)

    saved = await strict_cabinet.post(
        f"{CABINET_PREFIX}/password/{token}",
        data={"password": PASSWORD, "password_repeat": PASSWORD},
    )
    assert saved.status_code == 303
    assert strict_cabinet.cookies.get(SESSION_COOKIE_NAME)

    strict_cabinet.cookies.clear()
    accepted = await strict_cabinet.post(
        f"{CABINET_PREFIX}/login",
        data={"email": account.email, "password": PASSWORD},
    )
    assert accepted.status_code == 303


async def _issue_password_link(session, account) -> str:
    """Выдаёт ссылку для установки пароля напрямую, минуя интерфейсы."""
    from nosbp.cabinet import invites

    token = await invites.issue(
        session, account, purpose=InvitePurpose.PASSWORD, ttl_hours=72
    )
    await session.commit()
    return token


async def test_confirmation_link_is_stored_with_its_purpose(cabinet, mailbox, session):
    await _register(cabinet)

    invite = (
        await session.execute(
            select(AccountInvite).where(AccountInvite.purpose == InvitePurpose.EMAIL)
        )
    ).scalar_one()
    assert invite.used_at is None
