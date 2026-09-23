"""Тесты перевода копеек в рубли и обратно."""

from decimal import Decimal

from nosbp.core.money import KOPECKS_PER_ROUBLE, format_roubles, to_kopecks, to_roubles


def test_kopecks_to_roubles_is_exact():
    """Деньги считаются точно: Decimal, а не float."""
    assert to_roubles(10_01) == Decimal("10.01")
    assert isinstance(to_roubles(1), Decimal)


def test_roubles_to_kopecks():
    assert to_kopecks(1000) == 100_000
    assert to_kopecks(0) == 0


def test_conversion_round_trip():
    for roubles in (1, 47, 1000, 999_999):
        assert to_roubles(to_kopecks(roubles)) == Decimal(roubles)


def test_format_with_kopecks():
    assert format_roubles(123_45) == "123.45 ₽"
    assert format_roubles(-100) == "-1.00 ₽"


def test_format_without_kopecks():
    """Круглые суммы вроде лимитов читаются лучше без копеек."""
    assert format_roubles(100_000, fractional=False) == "1000 ₽"


def test_constant_matches_reality():
    assert KOPECKS_PER_ROUBLE == 100
