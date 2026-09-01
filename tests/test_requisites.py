"""Тесты проверки банковских реквизитов."""

import pytest

from nosbp.core.errors import ValidationError
from nosbp.payments.requisites import (
    validate_account,
    validate_bic,
    validate_inn,
    validate_kpp,
)
from tests.factories import (
    BIC,
    CORRESP_ACC,
    INN_COMPANY,
    INN_ENTREPRENEUR,
    PERSONAL_ACC,
)


def test_valid_bic_passes():
    assert validate_bic(BIC) == BIC


@pytest.mark.parametrize("value", ["04452522", "0445252251", "04452522a", ""])
def test_malformed_bic_is_rejected(value: str):
    with pytest.raises(ValidationError, match="БИК"):
        validate_bic(value)


def test_valid_settlement_account_passes():
    assert validate_account(PERSONAL_ACC, BIC) == PERSONAL_ACC


def test_valid_correspondent_account_passes():
    """У корсчёта другой префикс контрольной суммы — берётся из середины БИК."""
    assert validate_account(CORRESP_ACC, BIC) == CORRESP_ACC


def test_typo_in_account_is_caught():
    """Одна изменённая цифра ломает контрольную сумму — ради этого всё и делалось."""
    broken = PERSONAL_ACC[:-1] + "8"
    with pytest.raises(ValidationError, match="контрольного разряда"):
        validate_account(broken, BIC)


def test_account_of_wrong_length_is_rejected():
    with pytest.raises(ValidationError, match="20 цифр"):
        validate_account("4070281000", BIC)


def test_correct_account_with_wrong_bic_is_rejected():
    """Счёт проверяется в связке с банком, а не сам по себе."""
    with pytest.raises(ValidationError):
        validate_account(PERSONAL_ACC, "044525226")


def test_company_inn_passes():
    assert validate_inn(INN_COMPANY) == INN_COMPANY


def test_entrepreneur_inn_passes():
    assert validate_inn(INN_ENTREPRENEUR) == INN_ENTREPRENEUR


def test_inn_with_wrong_checksum_is_rejected():
    with pytest.raises(ValidationError, match="контрольной цифры"):
        validate_inn("7707083890")


@pytest.mark.parametrize("value", ["12345", "770708389012345"])
def test_inn_of_wrong_length_is_rejected(value: str):
    with pytest.raises(ValidationError, match="10 или 12"):
        validate_inn(value)


def test_empty_kpp_is_allowed():
    """У индивидуального предпринимателя КПП нет."""
    assert validate_kpp(None) is None
    assert validate_kpp("  ") is None


def test_malformed_kpp_is_rejected():
    with pytest.raises(ValidationError, match="КПП"):
        validate_kpp("7736010")
