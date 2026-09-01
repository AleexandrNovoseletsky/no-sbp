"""Формирование платёжной строки по ГОСТ Р 56042-2014.

Формат строки::

    ST00012|Name=ООО Ромашка|PersonalAcc=40702810…|BankName=…|BIC=…|…

Где ``ST0001`` — идентификатор и версия формата, а последняя цифра ``2``
означает кодировку UTF-8 (``1`` — Windows-1251, ``3`` — KOI8-R).
"""

from nosbp.core.errors import ValidationError
from nosbp.payments.schemas import PayeeRequisites, PaymentRequest

HEADER = "ST00012"
"""Служебный заголовок: формат ST0001, кодировка UTF-8."""

SEPARATOR = "|"

MAX_PAYLOAD_LENGTH = 300
"""Ограничение длины строки по ГОСТ Р 56042-2014."""


def _clean(value: str) -> str:
    """Приводит значение поля к виду, пригодному для строки ГОСТ.

    Разделитель ``|`` внутри значения разрушил бы всю строку, поэтому он
    заменяется на пробел. Переводы строк и повторяющиеся пробелы
    схлопываются: они не несут смысла, но занимают место в QR-коде.
    """
    return " ".join(value.replace(SEPARATOR, " ").split())


def build_gost_payload(
    requisites: PayeeRequisites,
    payment: PaymentRequest,
) -> str:
    """Собирает платёжную строку.

    :param requisites: реквизиты получателя (обязательная часть).
    :param payment: сумма, назначение и данные плательщика (необязательная).
    :raises ValidationError: если строка не помещается в ограничение ГОСТ.
    """
    # Порядок полей важен: сначала обязательные реквизиты в порядке ГОСТ,
    # затем дополнительные. Некоторые банковские приложения разбирают
    # строку позиционно и на другом порядке спотыкаются.
    fields: list[tuple[str, str | int | None]] = [
        ("Name", requisites.name),
        ("PersonalAcc", requisites.personal_acc),
        ("BankName", requisites.bank_name),
        ("BIC", requisites.bic),
        ("CorrespAcc", requisites.corresp_acc),
        ("PayeeINN", requisites.payee_inn),
        ("KPP", requisites.kpp),
        ("Sum", payment.sum_kopecks),
        ("Purpose", payment.purpose),
        ("LastName", payment.payer.last_name),
        ("FirstName", payment.payer.first_name),
        ("MiddleName", payment.payer.middle_name),
        ("Phone", payment.payer.phone),
    ]

    parts = [
        f"{key}={_clean(str(value))}" for key, value in fields if value is not None
    ]
    payload = HEADER + SEPARATOR + SEPARATOR.join(parts)

    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise ValidationError(
            f"Платёжная строка длиннее {MAX_PAYLOAD_LENGTH} символов "
            f"({len(payload)}). Сократите назначение платежа."
        )
    return payload
