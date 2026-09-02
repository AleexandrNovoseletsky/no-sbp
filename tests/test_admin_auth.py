"""Тесты входа в панель управления: пароль, второй фактор, сессии."""

import datetime

import pyotp
import pytest

from nosbp.admin.security import (
    SESSION_COOKIE_NAME,
    csrf_tokens_match,
    generate_csrf_token,
    hash_password,
    validate_password_strength,
    verify_password,
    verify_totp,
)
from nosbp.admin.service import AdminAuthService
from nosbp.core.config import get_settings
from nosbp.core.errors import AdminAuthError, AdminLockedError
from tests.factories import ADMIN_PREFIX


def settings_with(**overrides: object):
    return get_settings().model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Примитивы
# ---------------------------------------------------------------------------


def test_password_hash_is_not_the_password():
    hashed = hash_password("administrator-42-secret")
    assert "administrator-42-secret" not in hashed
    assert hashed.startswith("$argon2id$")


def test_password_verification():
    hashed = hash_password("administrator-42-secret")
    assert verify_password(hashed, "administrator-42-secret")
    assert not verify_password(hashed, "administrator-42-secreT")


def test_broken_hash_does_not_raise():
    """Испорченная строка в базе не должна ронять вход."""
    assert verify_password("не хэш вовсе", "что угодно") is False


@pytest.mark.parametrize("weak", ["короткий", "123456789012", "паролькороткий"])
def test_weak_passwords_are_rejected(weak: str):
    assert validate_password_strength(weak) is not None


def test_good_password_is_accepted():
    assert validate_password_strength("administrator-42-secret") is None


def test_totp_accepts_current_code():
    secret = pyotp.random_base32()
    assert verify_totp(secret, pyotp.TOTP(secret).now())


def test_totp_rejects_wrong_code():
    secret = pyotp.random_base32()
    assert not verify_totp(secret, "000000")
    assert not verify_totp(secret, "не цифры")


def test_totp_tolerates_spaces():
    """Аутентификаторы показывают код с пробелом посередине."""
    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret).now()
    assert verify_totp(secret, f"{code[:3]} {code[3:]}")


def test_csrf_comparison():
    token = generate_csrf_token()
    assert csrf_tokens_match(token, token)
    assert not csrf_tokens_match(token, token[:-1])
    assert not csrf_tokens_match(token, generate_csrf_token())
    assert not csrf_tokens_match(token, None)
    assert not csrf_tokens_match(token, "")


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


async def test_correct_credentials_authenticate(session, make_admin):
    admin, password, secret = await make_admin()
    service = AdminAuthService(session, get_settings())

    result = await service.authenticate(
        email=admin.email, password=password, totp_code=pyotp.TOTP(secret).now()
    )
    assert result.id == admin.id
    assert result.last_login_at is not None


async def test_email_is_case_insensitive(session, make_admin):
    admin, password, secret = await make_admin(email="admin@example.com")
    service = AdminAuthService(session, get_settings())

    result = await service.authenticate(
        email="  ADMIN@Example.COM  ",
        password=password,
        totp_code=pyotp.TOTP(secret).now(),
    )
    assert result.id == admin.id


async def test_wrong_password_is_rejected(session, make_admin):
    admin, _, secret = await make_admin()
    service = AdminAuthService(session, get_settings())

    with pytest.raises(AdminAuthError):
        await service.authenticate(
            email=admin.email, password="не тот", totp_code=pyotp.TOTP(secret).now()
        )


async def test_wrong_totp_is_rejected(session, make_admin):
    """Украденного пароля мало: нужен ещё код из аутентификатора."""
    admin, password, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    with pytest.raises(AdminAuthError):
        await service.authenticate(
            email=admin.email, password=password, totp_code="000000"
        )


async def test_unknown_email_gives_the_same_error(session):
    """Разные сообщения позволили бы перебрать заведённые адреса."""
    service = AdminAuthService(session, get_settings())
    with pytest.raises(AdminAuthError) as unknown:
        await service.authenticate(
            email="nobody@example.com", password="x" * 20, totp_code="000000"
        )
    assert "Неверная почта, пароль или одноразовый код" in unknown.value.message


async def test_disabled_admin_cannot_log_in(session, make_admin):
    admin, password, secret = await make_admin(is_active=False)
    service = AdminAuthService(session, get_settings())

    with pytest.raises(AdminAuthError):
        await service.authenticate(
            email=admin.email, password=password, totp_code=pyotp.TOTP(secret).now()
        )


async def test_totp_can_be_switched_off(session, make_admin):
    """На локальной машине второй фактор можно выключить настройкой."""
    admin, password, _ = await make_admin()
    service = AdminAuthService(session, settings_with(admin_require_totp=False))

    result = await service.authenticate(
        email=admin.email, password=password, totp_code=None
    )
    assert result.id == admin.id


# ---------------------------------------------------------------------------
# Блокировка перебора
# ---------------------------------------------------------------------------


async def test_repeated_failures_lock_the_account(session, make_admin):
    admin, password, secret = await make_admin()
    settings = settings_with(admin_max_login_attempts=3)
    service = AdminAuthService(session, settings)

    for _ in range(3):
        with pytest.raises(AdminAuthError):
            await service.authenticate(
                email=admin.email, password="не тот", totp_code="000000"
            )

    # Даже с верным паролем вход теперь закрыт.
    with pytest.raises(AdminLockedError, match="Повторите через"):
        await service.authenticate(
            email=admin.email, password=password, totp_code=pyotp.TOTP(secret).now()
        )


async def test_successful_login_clears_failures(session, make_admin):
    admin, password, secret = await make_admin()
    settings = settings_with(admin_max_login_attempts=5)
    service = AdminAuthService(session, settings)

    with pytest.raises(AdminAuthError):
        await service.authenticate(
            email=admin.email, password="не тот", totp_code="000000"
        )
    await service.authenticate(
        email=admin.email, password=password, totp_code=pyotp.TOTP(secret).now()
    )
    assert admin.failed_attempts == 0
    assert admin.locked_until is None


async def test_lock_expires(session, make_admin):
    admin, password, secret = await make_admin()
    service = AdminAuthService(session, get_settings())

    admin.locked_until = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=1
    )
    await session.commit()

    result = await service.authenticate(
        email=admin.email, password=password, totp_code=pyotp.TOTP(secret).now()
    )
    assert result.id == admin.id


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------


async def test_session_round_trip(session, make_admin):
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    issued = await service.open_session(
        admin, ip_address="127.0.0.1", user_agent="test"
    )
    loaded = await service.load_session(issued.token)

    assert loaded is not None
    assert loaded.admin_id == admin.id


async def test_session_token_is_stored_hashed(session, make_admin):
    """Из дампа базы рабочую сессию достать нельзя."""
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    issued = await service.open_session(admin, ip_address="", user_agent="")
    assert issued.token not in issued.session.token_hash
    assert len(issued.session.token_hash) == 64


async def test_unknown_token_gives_nothing(session, make_admin):
    service = AdminAuthService(session, get_settings())
    assert await service.load_session("выдуманный токен") is None
    assert await service.load_session(None) is None


async def test_closed_session_stops_working(session, make_admin):
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    issued = await service.open_session(admin, ip_address="", user_agent="")
    await service.close_session(issued.token)

    assert await service.load_session(issued.token) is None


async def test_expired_session_stops_working(session, make_admin):
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    issued = await service.open_session(admin, ip_address="", user_agent="")
    issued.session.expires_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(seconds=1)
    await session.commit()

    assert await service.load_session(issued.token) is None


async def test_idle_session_stops_working(session, make_admin):
    """Забытая открытой вкладка не должна оставаться входом навсегда."""
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, settings_with(admin_session_idle_minutes=30))

    issued = await service.open_session(admin, ip_address="", user_agent="")
    issued.session.last_seen_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(hours=2)
    await session.commit()

    assert await service.load_session(issued.token) is None


async def test_closing_all_sessions(session, make_admin):
    """Смена пароля обязана выкинуть того, кто увёл старый."""
    admin, _, _ = await make_admin()
    service = AdminAuthService(session, get_settings())

    tokens = [
        (await service.open_session(admin, ip_address="", user_agent="")).token
        for _ in range(3)
    ]
    closed = await service.close_all_sessions(admin.id)

    assert closed == 3
    for token in tokens:
        assert await service.load_session(token) is None


# ---------------------------------------------------------------------------
# Вход через HTTP
# ---------------------------------------------------------------------------


async def test_login_page_opens(panel):
    response = await panel.get(f"{ADMIN_PREFIX}/login")
    assert response.status_code == 200
    assert "Панель NoSBP" in response.text


async def test_login_sets_secure_cookie(panel, make_admin):
    admin, password, secret = await make_admin()
    response = await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={
            "email": admin.email,
            "password": password,
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert response.status_code == 303
    cookie_header = response.headers["set-cookie"]
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=strict" in cookie_header


async def test_failed_login_shows_form_again(panel, make_admin):
    admin, _, _ = await make_admin()
    response = await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={"email": admin.email, "password": "не тот", "totp_code": "000000"},
    )
    assert response.status_code == 200
    assert "Неверная почта" in response.text
    assert SESSION_COOKIE_NAME not in response.cookies


async def test_pages_require_login(panel):
    for path in ("/", "/accounts/new"):
        response = await panel.get(f"{ADMIN_PREFIX}{path}")
        assert response.status_code == 303
        assert "/login" in response.headers["location"]


async def test_logout_closes_session(logged_in):
    panel, _, csrf = logged_in
    response = await panel.post(f"{ADMIN_PREFIX}/logout", data={"csrf_token": csrf})
    assert response.status_code == 303

    after = await panel.get(f"{ADMIN_PREFIX}/")
    assert after.status_code == 303
    assert "/login" in after.headers["location"]
