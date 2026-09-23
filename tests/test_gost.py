"""Тесты формирования платёжной строки по ГОСТ Р 56042-2014."""

import pytest

from nosbp.core.errors import ValidationError
from nosbp.payments.gost import HEADER, build_gost_payload
from nosbp.payments.schemas import PayeeRequisites, PayerInfo, PaymentRequest
from tests.factories import (
    BIC,
    CORRESP_ACC,
    INN_COMPANY,
    KPP,
    PERSONAL_ACC,
)


def make_requisites(**overrides: object) -> PayeeRequisites:
    fields: dict[str, object] = {
        "name": "ООО Ромашка",
        "personal_acc": PERSONAL_ACC,
        "bank_name": "ПАО Сбербанк",
        "bic": BIC,
        "corresp_acc": CORRESP_ACC,
        "payee_inn": INN_COMPANY,
        "kpp": KPP,
    }
    fields.update(overrides)
    return PayeeRequisites(**fields)  # type: ignore[arg-type]


def test_payload_starts_with_gost_header():
    payload = build_gost_payload(make_requisites(), PaymentRequest())
    assert payload.startswith(HEADER + "|")


def test_mandatory_fields_go_in_gost_order():
    payload = build_gost_payload(make_requisites(), PaymentRequest())
    keys = [part.split("=", 1)[0] for part in payload.split("|")[1:]]
    assert keys[:6] == [
        "Name",
        "PersonalAcc",
        "BankName",
        "BIC",
        "CorrespAcc",
        "PayeeINN",
    ]


def test_kpp_is_included_when_present():
    payload = build_gost_payload(make_requisites(), PaymentRequest())
    assert f"KPP={KPP}" in payload


def test_kpp_is_omitted_for_entrepreneur():
    payload = build_gost_payload(make_requisites(kpp=None), PaymentRequest())
    assert "KPP=" not in payload


def test_empty_optional_fields_are_skipped():
    payload = build_gost_payload(make_requisites(), PaymentRequest())
    assert "Sum=" not in payload
    assert "Purpose=" not in payload
    assert "LastName=" not in payload


def test_sum_and_purpose_are_included():
    payment = PaymentRequest(sum_kopecks=4_700_000, purpose="Оплата заказа 1234")
    payload = build_gost_payload(make_requisites(), payment)
    assert "Sum=4700000" in payload
    assert "Purpose=Оплата заказа 1234" in payload


def test_payer_fields_are_included():
    payment = PaymentRequest(
        payer=PayerInfo(
            last_name="Иванов",
            first_name="Пётр",
            middle_name="Сергеевич",
            phone="+79001234567",
        )
    )
    payload = build_gost_payload(make_requisites(), payment)
    assert "LastName=Иванов" in payload
    assert "FirstName=Пётр" in payload
    assert "MiddleName=Сергеевич" in payload
    assert "Phone=+79001234567" in payload


def test_separator_inside_value_is_neutralised():
    """Вертикальная черта в значении разрушила бы всю строку."""
    payment = PaymentRequest(purpose="Заказ|1234")
    payload = build_gost_payload(make_requisites(), payment)
    assert "Purpose=Заказ 1234" in payload
    # Заголовок + 7 реквизитов + назначение: лишнего разделителя нет.
    assert len(payload.split("|")) == 9


def test_whitespace_is_collapsed():
    payment = PaymentRequest(purpose="Оплата   заказа\n1234")
    payload = build_gost_payload(make_requisites(), payment)
    assert "Purpose=Оплата заказа 1234" in payload


def test_too_long_payload_is_rejected():
    """ГОСТ ограничивает строку 300 символами — за этим следит валидатор."""
    payment = PaymentRequest(purpose="Ф" * 210)
    with pytest.raises(ValidationError, match="длиннее"):
        build_gost_payload(make_requisites(), payment)
