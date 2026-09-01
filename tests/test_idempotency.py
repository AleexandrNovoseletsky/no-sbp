"""Тесты ключа идемпотентности — механизма, на котором держится тарификация."""

import uuid

from nosbp.payments.idempotency import build_idempotency_key
from nosbp.payments.schemas import PayerInfo, PaymentRequest

ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


def key(payment: PaymentRequest, organization_id: uuid.UUID = ORG) -> str:
    return build_idempotency_key(organization_id=organization_id, payment=payment)


def test_key_is_sha256_hex():
    value = key(PaymentRequest())
    assert len(value) == 64
    assert all(char in "0123456789abcdef" for char in value)


def test_same_parameters_give_same_key():
    payment = PaymentRequest(sum_kopecks=4_700_000, purpose="Заказ 1234")
    assert key(payment) == key(
        PaymentRequest(sum_kopecks=4_700_000, purpose="Заказ 1234")
    )


def test_different_sum_gives_different_key():
    assert key(PaymentRequest(sum_kopecks=100)) != key(PaymentRequest(sum_kopecks=101))


def test_different_purpose_gives_different_key():
    assert key(PaymentRequest(purpose="Заказ 1")) != key(
        PaymentRequest(purpose="Заказ 2")
    )


def test_different_organization_gives_different_key():
    payment = PaymentRequest(sum_kopecks=100)
    assert key(payment) != key(payment, OTHER_ORG)


def test_different_payer_gives_different_key():
    """Два счёта на одну сумму разным людям — это два разных счёта."""
    first = PaymentRequest(payer=PayerInfo(last_name="Иванов"))
    second = PaymentRequest(payer=PayerInfo(last_name="Петров"))
    assert key(first) != key(second)


def test_extra_whitespace_does_not_create_new_invoice():
    """Случайный пробел в шаблоне CRM не должен стоить заказчику рубль."""
    assert key(PaymentRequest(purpose="Заказ 1234")) == key(
        PaymentRequest(purpose="  Заказ   1234  ")
    )


def test_case_matters():
    """Регистр — часть назначения платежа, а не шум."""
    assert key(PaymentRequest(purpose="Заказ")) != key(PaymentRequest(purpose="ЗАКАЗ"))


def test_field_shift_does_not_collide():
    """Перенос значения из одного поля в другое обязан менять ключ.

    Без разделителя между полями «Иванов»+«Пётр» и «Иван»+«овПётр»
    дали бы одинаковый хэш.
    """
    first = PaymentRequest(payer=PayerInfo(last_name="Иванов", first_name="Пётр"))
    second = PaymentRequest(payer=PayerInfo(last_name="Иван", first_name="овПётр"))
    assert key(first) != key(second)


def test_missing_and_empty_payer_are_the_same():
    assert key(PaymentRequest()) == key(PaymentRequest(payer=PayerInfo()))
