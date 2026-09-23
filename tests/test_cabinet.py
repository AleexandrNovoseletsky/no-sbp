"""Тесты личного кабинета заказчика."""

import datetime

import pytest
from sqlalchemy import select

from nosbp.cabinet.service import SESSION_COOKIE_NAME
from nosbp.db.models import Account, ApiToken, Organization
from tests.conftest import CABINET_PASSWORD
from tests.factories import (
    BIC,
    CABINET_PREFIX,
    CORRESP_ACC,
    INN_COMPANY,
    PERSONAL_ACC,
)

CABINET = CABINET_PREFIX


def org_form(csrf: str, **overrides: object) -> dict[str, object]:
    """Заполненная форма организации с корректными реквизитами."""
    return {
        "csrf_token": csrf,
        "alias": "main",
        "name": "ООО Ромашка",
        "personal_acc": PERSONAL_ACC,
        "bank_name": "ПАО Сбербанк",
        "bic": BIC,
        "corresp_acc": CORRESP_ACC,
        "payee_inn": INN_COMPANY,
        "kpp": "773601001",
        "qr_color": "#0e6b62",
        "fee_percent": "0,7",
        "average_check": "",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------


async def test_registration_page_opens(cabinet):
    response = await cabinet.get(f"{CABINET}/register")
    assert response.status_code == 200
    assert "Регистрация" in response.text


async def test_registration_creates_account_and_signs_in(cabinet, session):
    response = await cabinet.post(
        f"{CABINET}/register",
        data={
            "email": "shop@example.com",
            "display_name": "ООО Ромашка",
            "password": CABINET_PASSWORD,
            "password_repeat": CABINET_PASSWORD,
        },
    )
    assert response.status_code == 303
    assert cabinet.cookies.get(SESSION_COOKIE_NAME)

    account = (
        await session.execute(
            select(Account).where(Account.email == "shop@example.com")
        )
    ).scalar_one()
    assert account.display_name == "ООО Ромашка"
    assert account.password_hash is not None
    assert CABINET_PASSWORD not in account.password_hash


async def test_registration_normalises_email(cabinet, session):
    await cabinet.post(
        f"{CABINET}/register",
        data={
            "email": "  Shop@Example.COM ",
            "display_name": "ООО Ромашка",
            "password": CABINET_PASSWORD,
            "password_repeat": CABINET_PASSWORD,
        },
    )
    account = (
        await session.execute(
            select(Account).where(Account.email == "shop@example.com")
        )
    ).scalar_one()
    assert account.email == "shop@example.com"


async def test_duplicate_email_is_refused(cabinet, make_client_account):
    account, _ = await make_client_account(email="taken@example.com")

    response = await cabinet.post(
        f"{CABINET}/register",
        data={
            "email": account.email,
            "display_name": "Второй",
            "password": CABINET_PASSWORD,
            "password_repeat": CABINET_PASSWORD,
        },
    )
    assert response.status_code == 200
    assert "уже зарегистрирован" in response.text
    assert 'value="taken@example.com"' in response.text


async def test_mismatched_passwords_are_refused(cabinet):
    response = await cabinet.post(
        f"{CABINET}/register",
        data={
            "email": "shop@example.com",
            "display_name": "ООО Ромашка",
            "password": CABINET_PASSWORD,
            "password_repeat": "другой-пароль-12",
        },
    )
    assert response.status_code == 200
    assert "не совпадают" in response.text


@pytest.mark.parametrize("weak", ["короткий", "123456789012"])
async def test_weak_password_is_refused(cabinet, weak: str):
    response = await cabinet.post(
        f"{CABINET}/register",
        data={
            "email": "shop@example.com",
            "display_name": "ООО Ромашка",
            "password": weak,
            "password_repeat": weak,
        },
    )
    assert response.status_code == 200
    assert "Пароль должен" in response.text


async def test_registration_can_be_closed(storage, make_client_account):
    """Регистрацию можно закрыть настройкой, не трогая код."""
    from httpx import ASGITransport, AsyncClient

    from nosbp.core.config import get_settings
    from nosbp.main import create_app, prepare_state

    settings = get_settings().model_copy(update={"cabinet_registration_open": False})
    app = create_app(settings)
    app.state.storage = storage
    prepare_state(app, settings)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://cabinet.test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            f"{CABINET}/register",
            data={
                "email": "shop@example.com",
                "display_name": "ООО Ромашка",
                "password": CABINET_PASSWORD,
                "password_repeat": CABINET_PASSWORD,
            },
        )
    assert response.status_code == 200
    assert "закрыта" in response.text


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


async def test_login_page_opens(cabinet):
    response = await cabinet.get(f"{CABINET}/login")
    assert response.status_code == 200
    assert "Вход" in response.text


async def test_login_sets_secure_cookie(cabinet, make_client_account):
    account, password = await make_client_account()
    response = await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": password}
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie


async def test_wrong_password_is_refused(cabinet, make_client_account):
    account, _ = await make_client_account()
    response = await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": "не тот"}
    )
    assert response.status_code == 200
    assert "Неверная почта или пароль" in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


async def test_unknown_email_gives_the_same_error(cabinet):
    """Разные сообщения позволили бы перебрать зарегистрированные адреса."""
    response = await cabinet.post(
        f"{CABINET}/login",
        data={"email": "nobody@example.com", "password": CABINET_PASSWORD},
    )
    assert "Неверная почта или пароль" in response.text


async def test_disabled_account_cannot_sign_in(cabinet, make_client_account):
    account, password = await make_client_account(is_active=False)
    response = await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": password}
    )
    assert response.status_code == 200
    assert "отключена" in response.text


async def test_repeated_failures_lock_the_account(
    cabinet, session, make_client_account
):
    account, password = await make_client_account()
    for _ in range(10):
        await cabinet.post(
            f"{CABINET}/login", data={"email": account.email, "password": "не тот"}
        )

    response = await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": password}
    )
    assert "Повторите через" in response.text


async def test_pages_require_sign_in(cabinet):
    for path in ("/", "/statistics", "/balance", "/organizations/new"):
        response = await cabinet.get(f"{CABINET}{path}")
        assert response.status_code == 303, path
        assert "/login" in response.headers["location"]


async def test_logout_closes_session(signed_in):
    cabinet, _, csrf = signed_in
    response = await cabinet.post(f"{CABINET}/logout", data={"csrf_token": csrf})
    assert response.status_code == 303

    after = await cabinet.get(f"{CABINET}/")
    assert after.status_code == 303
    assert "/login" in after.headers["location"]


# ---------------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------------


async def test_dashboard_opens(signed_in):
    cabinet, account, _ = signed_in
    response = await cabinet.get(f"{CABINET}/")
    assert response.status_code == 200
    assert account.display_name in response.text
    assert "Как подключить" in response.text
    assert "Организаций пока нет" in response.text


async def test_statistics_opens(signed_in):
    cabinet, _, _ = signed_in
    response = await cabinet.get(f"{CABINET}/statistics")
    assert response.status_code == 200
    assert "счетов выставлено" in response.text
    assert "История счетов" in response.text


async def test_balance_opens(signed_in):
    cabinet, _, _ = signed_in
    response = await cabinet.get(f"{CABINET}/balance")
    assert response.status_code == 200
    assert "Выписка" in response.text
    assert "Операций пока не было" in response.text


async def test_unlimited_account_sees_no_top_up_offer(
    cabinet, session, make_client_account
):
    account, password = await make_client_account(is_unlimited=True)
    await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": password}
    )

    response = await cabinet.get(f"{CABINET}/balance")
    assert "безлимит" in response.text
    assert "без списаний" in response.text


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


async def test_create_organization(signed_in, session):
    cabinet, account, csrf = signed_in
    response = await cabinet.post(
        f"{CABINET}/organizations/new", data=org_form(csrf, is_default="1")
    )
    assert response.status_code == 303

    organization = (
        await session.execute(
            select(Organization).where(Organization.account_id == account.id)
        )
    ).scalar_one()
    assert organization.alias == "main"
    assert organization.acquiring_fee_bps == 70
    assert organization.is_default


async def test_typo_in_account_is_refused_and_form_kept(signed_in, session):
    cabinet, account, csrf = signed_in
    broken = PERSONAL_ACC[:-1] + "8"

    response = await cabinet.post(
        f"{CABINET}/organizations/new",
        data=org_form(csrf, personal_acc=broken, name="ООО Ошибка"),
    )
    assert response.status_code == 200
    assert "контрольного разряда" in response.text
    assert f'value="{broken}"' in response.text
    assert 'value="ООО Ошибка"' in response.text

    left = await session.execute(
        select(Organization).where(Organization.account_id == account.id)
    )
    assert left.scalars().first() is None


async def test_edit_organization(signed_in, session, make_organization):
    cabinet, account, csrf = signed_in
    organization = await make_organization(account, alias="first")

    response = await cabinet.post(
        f"{CABINET}/organizations/{organization.id}/edit",
        data=org_form(csrf, alias="second", name="ИП Иванов", fee_percent="2,5"),
    )
    assert response.status_code == 303

    await session.refresh(organization)
    assert organization.alias == "second"
    assert organization.acquiring_fee_bps == 250


async def test_delete_organization(signed_in, session, make_organization):
    cabinet, account, csrf = signed_in
    organization = await make_organization(account)

    response = await cabinet.post(
        f"{CABINET}/organizations/{organization.id}/delete",
        data={"csrf_token": csrf},
    )
    assert response.status_code == 303

    session.expunge_all()
    assert await session.get(Organization, organization.id) is None


# ---------------------------------------------------------------------------
# Доступ только к своим данным
# ---------------------------------------------------------------------------


async def test_foreign_organization_is_not_visible(
    signed_in, make_account, make_organization
):
    """Подстановка чужого идентификатора не должна открывать чужие реквизиты."""
    cabinet, _, _ = signed_in
    stranger = await make_account()
    foreign = await make_organization(stranger, alias="foreign")

    response = await cabinet.get(f"{CABINET}/organizations/{foreign.id}")
    assert response.status_code == 404
    assert "не найдена" in response.text


async def test_foreign_organization_cannot_be_edited(
    signed_in, session, make_account, make_organization
):
    cabinet, _, csrf = signed_in
    stranger = await make_account()
    foreign = await make_organization(stranger, alias="foreign", name="Чужая")

    response = await cabinet.post(
        f"{CABINET}/organizations/{foreign.id}/edit",
        data=org_form(csrf, name="Взломано"),
    )
    assert response.status_code == 404

    await session.refresh(foreign)
    assert foreign.name == "Чужая"


async def test_foreign_organization_cannot_be_deleted(
    signed_in, session, make_account, make_organization
):
    cabinet, _, csrf = signed_in
    stranger = await make_account()
    foreign = await make_organization(stranger, alias="foreign")

    response = await cabinet.post(
        f"{CABINET}/organizations/{foreign.id}/delete", data={"csrf_token": csrf}
    )
    assert response.status_code == 404

    session.expunge_all()
    assert await session.get(Organization, foreign.id) is not None


async def test_foreign_token_cannot_be_revoked(
    signed_in, session, make_account, make_token
):
    cabinet, _, csrf = signed_in
    stranger = await make_account()
    await make_token(stranger)

    foreign_token = (
        await session.execute(
            select(ApiToken).where(ApiToken.account_id == stranger.id)
        )
    ).scalar_one()

    response = await cabinet.post(
        f"{CABINET}/tokens/{foreign_token.id}/revoke", data={"csrf_token": csrf}
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]

    await session.refresh(foreign_token)
    assert foreign_token.revoked_at is None


async def test_statistics_show_only_own_data(
    signed_in, session, make_account, make_organization
):
    cabinet, _, _ = signed_in
    stranger = await make_account()
    await make_organization(stranger, alias="chuzhaya", name="Чужая компания")

    response = await cabinet.get(f"{CABINET}/statistics")
    assert "Чужая компания" not in response.text
    assert "chuzhaya" not in response.text


# ---------------------------------------------------------------------------
# Ключи
# ---------------------------------------------------------------------------


async def test_issue_and_revoke_token(signed_in, session):
    cabinet, account, csrf = signed_in

    issued = await cabinet.post(
        f"{CABINET}/tokens", data={"csrf_token": csrf, "label": "Сайт"}
    )
    assert issued.status_code == 303
    assert "token=" in issued.headers["location"]

    api_token = (
        await session.execute(select(ApiToken).where(ApiToken.account_id == account.id))
    ).scalar_one()
    assert api_token.label == "Сайт"

    revoked = await cabinet.post(
        f"{CABINET}/tokens/{api_token.id}/revoke", data={"csrf_token": csrf}
    )
    assert revoked.status_code == 303

    await session.refresh(api_token)
    assert api_token.revoked_at is not None


async def test_issued_key_actually_works(signed_in, session, make_organization):
    """Ключ из кабинета должен сразу годиться для генерации QR-кода."""
    from httpx import ASGITransport, AsyncClient

    cabinet, account, csrf = signed_in
    await make_organization(account)

    from nosbp.billing.service import BillingService
    from nosbp.core.config import get_settings

    billing = BillingService(session, get_settings())
    locked = await billing.lock_account(account.id)
    await billing.adjust(locked, 100_000, comment="Тест")
    await session.commit()

    issued = await cabinet.post(f"{CABINET}/tokens", data={"csrf_token": csrf})
    token = issued.headers["location"].split("token=")[1].split("&")[0]

    async with AsyncClient(
        transport=ASGITransport(app=cabinet._transport.app),  # type: ignore[attr-defined]
        base_url="https://cabinet.test",
    ) as api:
        response = await api.get(f"/v1/qr?token={token}&sum=4700000")

    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# Защита форм
# ---------------------------------------------------------------------------


async def test_form_without_csrf_is_refused(signed_in, session):
    cabinet, account, _ = signed_in
    response = await cabinet.post(f"{CABINET}/tokens", data={"label": "без токена"})

    assert response.status_code == 303
    assert "/login" in response.headers["location"]

    left = await session.execute(
        select(ApiToken).where(ApiToken.account_id == account.id)
    )
    assert left.scalars().first() is None


# ---------------------------------------------------------------------------
# Смена пароля
# ---------------------------------------------------------------------------


async def test_password_change_closes_sessions(signed_in, session):
    cabinet, account, csrf = signed_in
    new_password = "novyj-parol-2026"

    response = await cabinet.post(
        f"{CABINET}/password",
        data={
            "csrf_token": csrf,
            "current_password": CABINET_PASSWORD,
            "new_password": new_password,
            "new_password_repeat": new_password,
        },
    )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]

    after = await cabinet.get(f"{CABINET}/")
    assert after.status_code == 303

    signed = await cabinet.post(
        f"{CABINET}/login",
        data={"email": account.email, "password": new_password},
    )
    assert signed.status_code == 303


async def test_wrong_current_password_is_refused(signed_in, session):
    cabinet, account, csrf = signed_in
    response = await cabinet.post(
        f"{CABINET}/password",
        data={
            "csrf_token": csrf,
            "current_password": "не тот",
            "new_password": "novyj-parol-2026",
            "new_password_repeat": "novyj-parol-2026",
        },
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]

    await session.refresh(account)
    from nosbp.core.security import verify_password

    assert verify_password(account.password_hash or "", CABINET_PASSWORD)


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------


async def test_idle_session_expires(signed_in, session):
    from nosbp.db.models import AccountSession

    cabinet, account, _ = signed_in
    account_session = (
        await session.execute(
            select(AccountSession).where(AccountSession.owner_id == account.id)
        )
    ).scalar_one()

    account_session.last_seen_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(days=1)
    await session.commit()

    response = await cabinet.get(f"{CABINET}/")
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


async def test_negative_balance_is_flagged(cabinet, session, make_client_account):
    """Заказчик должен видеть, что работает в долг, а не только цифру."""
    import datetime

    account, password = await make_client_account()
    account.balance_kopecks = -500
    account.overdraft_until = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        days=2
    )
    await session.commit()

    await cabinet.post(
        f"{CABINET}/login", data={"email": account.email, "password": password}
    )
    response = await cabinet.get(f"{CABINET}/")

    assert "Баланс отрицательный" in response.text
    assert "перестанут выдаваться" in response.text
