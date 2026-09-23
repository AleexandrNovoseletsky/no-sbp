"""Тесты почтового слоя: тексты писем, сборка сообщения и устойчивость."""

import datetime

import pytest

from nosbp.core.config import get_settings
from nosbp.core.constants import SmtpSecurity
from nosbp.mail import letters
from nosbp.mail.base import Letter
from nosbp.mail.render import render_html, render_text
from nosbp.mail.service import Mailer, build_sender
from nosbp.mail.smtp import NullSender, SmtpSender

SERVICE = "NoSBP"
BASE_URL = "https://nosbp.ru"
LINK = "https://nosbp.ru/cabinet/password/TOKEN"


def _render(letter: Letter) -> tuple[str, str]:
    """Обе версии письма разом."""
    return (
        render_text(letter, service_name=SERVICE, base_url=BASE_URL),
        render_html(letter, service_name=SERVICE, base_url=BASE_URL),
    )


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------


def test_reset_letter_carries_the_link_in_both_versions():
    text, html = _render(letters.password_reset(url=LINK, ttl_hours=1))

    assert LINK in text
    assert LINK in html


def test_reset_letter_offers_a_link_and_nothing_else():
    """В письме только ссылка: ни пароля, ни формы ввода."""
    text, html = _render(letters.password_reset(url=LINK, ttl_hours=1))

    assert "Задать новый пароль" in text
    assert "<form" not in html
    assert "<input" not in html


def test_reset_letter_states_the_lifetime():
    text, _ = _render(letters.password_reset(url=LINK, ttl_hours=1))
    assert "1 час." in text


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (1, "1 час"),
        (2, "2 часа"),
        (5, "5 часов"),
        (11, "11 часов"),
        (21, "21 час"),
        (48, "48 часов"),
        (72, "72 часа"),
    ],
)
def test_hours_are_declined(hours, expected):
    """«Действует 1 часов» в письме про безопасность выглядит подделкой."""
    text, _ = _render(letters.password_reset(url=LINK, ttl_hours=hours))
    assert f"Ссылка действует {expected}." in text


def test_invite_letter_explains_that_the_account_was_created_for_them():
    text, _ = _render(letters.access_invite(url=LINK, ttl_hours=72))
    assert "заведён аккаунт" in text
    assert LINK in text


def test_confirmation_letter_leads_to_its_own_link():
    text, _ = _render(letters.email_confirmation(url=LINK, ttl_hours=48))
    assert "Подтвердить адрес" in text
    assert LINK in text


def test_password_changed_letter_has_no_action():
    letter = letters.password_changed(
        moment=datetime.datetime(2026, 5, 1, 12, 30, tzinfo=datetime.UTC),
        address="203.0.113.7",
        support_email="help@nosbp.ru",
    )
    text, _ = _render(letter)

    assert not letter.action_url
    assert "203.0.113.7" in text
    assert "help@nosbp.ru" in text
    assert "01.05.2026 в 12:30" in text


def test_html_escapes_values_from_outside():
    """Ссылка и тексты попадают в разметку — экранирование обязательно."""
    _, html = _render(
        Letter(
            subject="тема",
            heading="<script>alert(1)</script>",
            paragraphs=("обычный абзац",),
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Сборка сообщения
# ---------------------------------------------------------------------------


def _sender(**overrides) -> SmtpSender:
    fields = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "robot@example.com",
        "password": "secret",
        "security": SmtpSecurity.SSL,
        "sender_address": "robot@example.com",
        "sender_name": "NoSBP",
        "timeout": 5.0,
    }
    return SmtpSender(**{**fields, **overrides})


def test_message_has_both_parts_and_a_readable_sender():
    message = _sender()._build(
        to="client@example.com", subject="Тема", text="текст", html="<b>текст</b>"
    )

    assert message["To"] == "client@example.com"
    assert message["From"] == "NoSBP <robot@example.com>"
    assert message["Subject"] == "Тема"
    assert message["Auto-Submitted"] == "auto-generated"

    parts = [part.get_content_type() for part in message.walk()]
    assert "text/plain" in parts
    assert "text/html" in parts


def test_message_id_uses_the_sender_domain():
    """Иначе в идентификатор попадает имя хоста, и письмо ловят фильтры."""
    message = _sender()._build(to="c@example.com", subject="s", text="t", html="h")
    assert message["Message-ID"].endswith("@example.com>")


def test_sender_without_host_is_not_configured():
    assert not _sender(host="").is_configured
    assert not _sender(sender_address="").is_configured


# ---------------------------------------------------------------------------
# Устойчивость
# ---------------------------------------------------------------------------


async def test_mailer_survives_a_broken_channel():
    """Отказ почты не должен ронять операцию, которая её вызвала."""

    class Broken:
        name = "broken"
        is_configured = True

        async def send(self, *, to, subject, text, html):
            raise ConnectionRefusedError("сервер недоступен")

    mailer = Mailer(Broken(), base_url=BASE_URL)
    assert (
        await mailer.send(
            "c@example.com", letters.password_reset(url=LINK, ttl_hours=1)
        )
        is False
    )


async def test_null_sender_reports_that_nothing_was_delivered():
    mailer = Mailer(NullSender(), base_url=BASE_URL)

    assert mailer.is_configured is False
    assert (
        await mailer.send(
            "c@example.com", letters.email_confirmation(url=LINK, ttl_hours=1)
        )
        is False
    )


def test_sender_falls_back_to_null_when_not_configured():
    settings = get_settings().model_copy(update={"smtp_host": ""})
    assert isinstance(build_sender(settings), NullSender)


def test_sender_uses_smtp_when_configured():
    settings = get_settings().model_copy(
        update={"smtp_host": "smtp.example.com", "smtp_user": "robot@example.com"}
    )
    assert isinstance(build_sender(settings), SmtpSender)
