"""Тексты писем.

Все письма собраны в одном модуле: так видно весь набор того, что сервис
пишет заказчику, и легко выдержать общий тон. Ни в одном письме нет пароля
и никаких данных плательщиков — только ссылка на действие.
"""

import datetime
from typing import Final

from nosbp.mail.base import Letter

DATETIME_FORMAT: Final[str] = "%d.%m.%Y в %H:%M"
"""Время в письмах пишется словами, а не в машинном формате."""

LINK_IS_SINGLE_USE: Final[str] = (
    "Ссылка одноразовая: после установки пароля она перестанет работать."
)


def _hours(count: int) -> str:
    """Склоняет слово «час» по правилам русского языка.

    Мелочь, но письмо со строкой «действует 1 часов» выглядит как подделка,
    а это письмо про безопасность.
    """
    tail_two = count % 100
    if 11 <= tail_two <= 14:
        return f"{count} часов"

    tail = count % 10
    if tail == 1:
        return f"{count} час"
    if 2 <= tail <= 4:
        return f"{count} часа"
    return f"{count} часов"


def password_reset(*, url: str, ttl_hours: int) -> Letter:
    """Письмо по запросу восстановления пароля."""
    return Letter(
        subject="Восстановление пароля в NoSBP",
        heading="Восстановление пароля",
        paragraphs=(
            "Кто-то запросил установку нового пароля для этого адреса. "
            "Если это были вы — нажмите кнопку ниже.",
        ),
        action_label="Задать новый пароль",
        action_url=url,
        footer=(
            f"Ссылка действует {_hours(ttl_hours)}.",
            LINK_IS_SINGLE_USE,
            "Если вы не запрашивали смену пароля, просто удалите это "
            "письмо — текущий пароль останется прежним.",
        ),
    )


def access_invite(*, url: str, ttl_hours: int) -> Letter:
    """Письмо с доступом в кабинет для аккаунта, заведённого оператором."""
    return Letter(
        subject="Доступ в личный кабинет NoSBP",
        heading="Вход в личный кабинет",
        paragraphs=(
            "Для вас заведён аккаунт в NoSBP — сервисе генерации QR-кодов "
            "для оплаты по реквизитам.",
            "Пароль пока не задан. Задайте его по ссылке, и кабинет "
            "откроется сразу после сохранения.",
        ),
        action_label="Задать пароль и войти",
        action_url=url,
        footer=(
            f"Ссылка действует {_hours(ttl_hours)}.",
            LINK_IS_SINGLE_USE,
        ),
    )


def email_confirmation(*, url: str, ttl_hours: int) -> Letter:
    """Письмо с подтверждением адреса при регистрации."""
    return Letter(
        subject="Подтверждение адреса почты в NoSBP",
        heading="Подтвердите адрес почты",
        paragraphs=(
            "Этот адрес указан при регистрации в NoSBP. Подтвердите его, "
            "чтобы мы могли восстановить вам доступ, если пароль потеряется.",
        ),
        action_label="Подтвердить адрес",
        action_url=url,
        footer=(
            f"Ссылка действует {_hours(ttl_hours)}.",
            "Если вы не регистрировались в NoSBP, удалите это письмо: "
            "без подтверждения адрес не будет использован.",
        ),
    )


def password_changed(
    *, moment: datetime.datetime, address: str, support_email: str
) -> Letter:
    """Уведомление о состоявшейся смене пароля.

    Письмо без действия: единственный его смысл — чтобы владелец адреса
    заметил смену пароля, которую делал не он.
    """
    footer = ["Все открытые сеансы при этом были завершены."]
    if support_email:
        footer.append(f"Если пароль меняли не вы — напишите на {support_email}.")

    return Letter(
        subject="Пароль в NoSBP изменён",
        heading="Пароль изменён",
        paragraphs=(
            f"Пароль от личного кабинета изменён "
            f"{moment.strftime(DATETIME_FORMAT)} UTC.",
            f"Запрос пришёл с адреса {address or 'неизвестного'}.",
        ),
        footer=tuple(footer),
    )
