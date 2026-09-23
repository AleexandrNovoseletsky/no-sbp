"""Тесты оповещений о событиях безопасности."""

import datetime

import httpx
import pyotp
import pytest

from nosbp.core.config import Settings, get_settings
from nosbp.notifications.base import NotificationChannel
from nosbp.notifications.max_messenger import MaxChannel
from nosbp.notifications.service import (
    Notifier,
    admin_login_failed_message,
    admin_login_message,
    build_channels,
)
from nosbp.notifications.telegram import TelegramChannel
from tests.factories import ADMIN_PREFIX

MOMENT = datetime.datetime(2026, 9, 3, 14, 30, 15, tzinfo=datetime.UTC)


class RecordingChannel(NotificationChannel):
    """Канал, запоминающий отправленное."""

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[str] = []
        self._fail = fail

    @property
    def name(self) -> str:
        return "recording"

    async def send(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("канал недоступен")
        self.messages.append(text)


def settings_with(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Подключение каналов
# ---------------------------------------------------------------------------


def test_no_channels_without_configuration():
    assert build_channels(Settings()) == ()


def test_channel_needs_both_token_and_chat():
    """Половина настроек не должна включать канал."""
    assert build_channels(Settings(telegram_bot_token="t")) == ()
    assert build_channels(Settings(telegram_chat_id="1")) == ()


def test_channels_are_built_from_settings():
    channels = build_channels(
        Settings(
            telegram_bot_token="t",
            telegram_chat_id="1",
            max_bot_token="m",
            max_chat_id="2",
        )
    )
    assert [channel.name for channel in channels] == ["telegram", "max"]


def test_notifier_reports_configuration():
    assert not Notifier(()).is_configured
    assert Notifier([RecordingChannel()]).is_configured


# ---------------------------------------------------------------------------
# Рассылка
# ---------------------------------------------------------------------------


async def test_message_goes_to_every_channel():
    first, second = RecordingChannel(), RecordingChannel()
    await Notifier([first, second]).send("привет")

    assert first.messages == ["привет"]
    assert second.messages == ["привет"]


async def test_broken_channel_does_not_stop_the_rest():
    """Отказ мессенджера не должен влиять на остальные каналы."""
    broken, working = RecordingChannel(fail=True), RecordingChannel()
    await Notifier([broken, working]).send("привет")

    assert working.messages == ["привет"]


async def test_failure_is_not_raised():
    """Ошибка доставки не должна прерывать операцию, которая её вызвала."""
    await Notifier([RecordingChannel(fail=True)]).send("привет")


# ---------------------------------------------------------------------------
# Текст сообщений
# ---------------------------------------------------------------------------


def test_login_message_contains_the_essentials():
    text = admin_login_message(
        email="boss@example.com",
        address="203.0.113.7",
        user_agent="Mozilla/5.0",
        moment=MOMENT,
        base_url="https://example.com",
    )
    assert "Вход в панель управления" in text
    assert "boss@example.com" in text
    assert "203.0.113.7" in text
    assert "Mozilla/5.0" in text
    assert "03.09.2026 14:30:15" in text
    assert "https://example.com" in text


def test_failed_login_message_marks_lockout():
    locked = admin_login_failed_message(
        email="boss@example.com",
        address="203.0.113.7",
        user_agent="",
        moment=MOMENT,
        base_url="https://example.com",
        locked=True,
    )
    plain = admin_login_failed_message(
        email="boss@example.com",
        address="203.0.113.7",
        user_agent="",
        moment=MOMENT,
        base_url="https://example.com",
        locked=False,
    )
    assert "заблокирован" in locked
    assert "unlock" in locked
    assert "Неудачная попытка" in plain


def test_message_escapes_markup():
    """Значения приходят из запроса, а сообщение отправляется с разметкой."""
    text = admin_login_message(
        email="<b>boss</b>@example.com",
        address="203.0.113.7",
        user_agent="<script>alert(1)</script>",
        moment=MOMENT,
        base_url="https://example.com",
    )
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;boss&lt;/b&gt;" in text


def test_long_user_agent_is_trimmed():
    text = admin_login_message(
        email="boss@example.com",
        address="203.0.113.7",
        user_agent="A" * 500,
        moment=MOMENT,
        base_url="https://example.com",
    )
    assert "A" * 500 not in text


def test_missing_values_have_placeholders():
    text = admin_login_message(
        email="boss@example.com",
        address="",
        user_agent="",
        moment=MOMENT,
        base_url="https://example.com",
    )
    assert "неизвестен" in text
    assert "не указан" in text


# ---------------------------------------------------------------------------
# Запросы к API мессенджеров
# ---------------------------------------------------------------------------


async def test_telegram_request_shape(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await TelegramChannel(token="SECRET", chat_id="42", timeout=1).send("текст")

    assert captured["url"] == "https://api.telegram.org/botSECRET/sendMessage"
    assert captured["json"] == {
        "chat_id": "42",
        "text": "текст",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


async def test_max_request_shape(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["json"] = kwargs.get("json")
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await MaxChannel(token="SECRET", chat_id="42", timeout=1).send("текст")

    assert captured["url"] == "https://botapi.max.ru/messages"
    assert captured["params"] == {"access_token": "SECRET", "chat_id": "42"}
    assert captured["json"] == {"text": "текст"}


async def test_http_error_is_reported_as_failure(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    channel = TelegramChannel(token="SECRET", chat_id="42", timeout=1)

    with pytest.raises(httpx.HTTPStatusError):
        await channel.send("текст")

    # Через рассылку та же ошибка гасится.
    await Notifier([channel]).send("текст")


# ---------------------------------------------------------------------------
# Вход в панель
# ---------------------------------------------------------------------------


async def test_successful_login_sends_notification(panel, make_admin):
    admin, password, secret = await make_admin()
    channel = RecordingChannel()
    panel._transport.app.state.notifier = Notifier([channel])  # type: ignore[attr-defined]

    response = await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={
            "email": admin.email,
            "password": password,
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert response.status_code == 303
    assert len(channel.messages) == 1
    assert admin.email in channel.messages[0]
    assert "Вход в панель управления" in channel.messages[0]


async def test_failed_login_sends_notification(panel, make_admin):
    admin, _, _ = await make_admin()
    channel = RecordingChannel()
    panel._transport.app.state.notifier = Notifier([channel])  # type: ignore[attr-defined]

    await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={"email": admin.email, "password": "не тот", "totp_code": "000000"},
    )
    assert len(channel.messages) == 1
    assert "Неудачная попытка" in channel.messages[0]


async def test_notification_never_leaks_the_password(panel, make_admin):
    admin, _, _ = await make_admin()
    channel = RecordingChannel()
    panel._transport.app.state.notifier = Notifier([channel])  # type: ignore[attr-defined]

    await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={
            "email": admin.email,
            "password": "очень-секретный-пароль",
            "totp_code": "000000",
        },
    )
    assert "очень-секретный-пароль" not in channel.messages[0]


async def test_broken_notifier_does_not_break_login(panel, make_admin):
    """Недоступный мессенджер не должен мешать войти в панель."""
    admin, password, secret = await make_admin()
    panel._transport.app.state.notifier = Notifier(  # type: ignore[attr-defined]
        [RecordingChannel(fail=True)]
    )

    response = await panel.post(
        f"{ADMIN_PREFIX}/login",
        data={
            "email": admin.email,
            "password": password,
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert response.status_code == 303


async def test_notifications_can_be_switched_off(make_admin, storage):
    """Оповещения отключаются настройкой, каналы при этом не трогаются."""
    from httpx import ASGITransport, AsyncClient

    from nosbp.main import create_app, prepare_state

    admin, password, secret = await make_admin()
    settings = settings_with(notify_admin_login=False)

    app = create_app(settings)
    app.state.storage = storage
    prepare_state(app, settings)

    channel = RecordingChannel()
    app.state.notifier = Notifier([channel])

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://panel.test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            f"{ADMIN_PREFIX}/login",
            data={
                "email": admin.email,
                "password": password,
                "totp_code": pyotp.TOTP(secret).now(),
            },
        )

    assert response.status_code == 303
    assert channel.messages == []
