"""Проверка банковских реквизитов.

Все эти проверки выполняются один раз — когда заказчик сохраняет
организацию. Смысл в том, чтобы поймать опечатку в момент ввода,
а не тогда, когда деньги уже ушли на несуществующий счёт.

На горячем пути генерации QR ничего отсюда не вызывается.
"""

from itertools import cycle
from typing import Final

from nosbp.core.constants import (
    ACCOUNT_LENGTH,
    BIC_LENGTH,
    INN_LENGTHS,
    KPP_LENGTH,
)
from nosbp.core.errors import ValidationError

CORRESPONDENT_ACCOUNT_PREFIX: Final[str] = "30101"
"""С этого начинаются корреспондентские счета — у них другой алгоритм."""

ACCOUNT_CHECKSUM_WEIGHTS: Final[tuple[int, ...]] = (7, 1, 3)
"""Веса разрядов при расчёте контрольной суммы счёта."""

DECIMAL_MODULUS: Final = 10
"""Последний шаг обеих контрольных сумм — остаток от деления на десять."""

INN_COMPANY_LENGTH: Final = 10
INN_COMPANY_WEIGHTS: Final[tuple[int, ...]] = (2, 4, 10, 3, 5, 9, 4, 6, 8)
INN_PERSON_WEIGHTS_FIRST: Final[tuple[int, ...]] = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
INN_PERSON_WEIGHTS_SECOND: Final[tuple[int, ...]] = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
INN_CHECKSUM_MODULUS: Final = 11
"""Веса и модуль контрольных цифр ИНН по приказу ФНС.

Вынесены на уровень модуля: это неизменяемые таблицы, и пересобирать их
при каждом вызове незачем.
"""


def validate_bic(bic: str) -> str:
    """Проверяет БИК: девять цифр.

    :return: БИК без окружающих пробелов, если он корректен.
    :raises ValidationError: если формат нарушен.
    """
    value = bic.strip()
    if len(value) != BIC_LENGTH or not value.isdigit():
        raise ValidationError(
            f"БИК «{bic}» должен состоять ровно из {BIC_LENGTH} цифр."
        )
    return value


def validate_account(account: str, bic: str) -> str:
    """Проверяет расчётный или корреспондентский счёт по контрольному разряду.

    Алгоритм ЦБ РФ: к счёту слева приписывается трёхзначный префикс,
    зависящий от типа счёта, затем считается взвешенная сумма разрядов.
    Если она не делится на десять — в счёте опечатка.

    :param account: номер счёта, 20 цифр.
    :param bic: БИК банка, в котором открыт счёт.
    :raises ValidationError: если формат или контрольная сумма не сходятся.
    """
    value = account.strip()
    if len(value) != ACCOUNT_LENGTH or not value.isdigit():
        raise ValidationError(
            f"Счёт «{account}» должен состоять ровно из {ACCOUNT_LENGTH} цифр."
        )

    checked_bic = validate_bic(bic)

    if value.startswith(CORRESPONDENT_ACCOUNT_PREFIX):
        # Для корсчёта берутся пятый и шестой разряды БИК.
        prefix = "0" + checked_bic[4:6]
    else:
        # Для расчётного счёта — условный номер подразделения банка,
        # то есть последние три разряда БИК.
        prefix = checked_bic[6:9]

    # cycle повторяет веса по кругу — так длина таблицы вычисляется один
    # раз, а не на каждом разряде внутри цикла.
    checksum = sum(
        int(digit) * weight
        for digit, weight in zip(prefix + value, cycle(ACCOUNT_CHECKSUM_WEIGHTS))
    )
    if checksum % DECIMAL_MODULUS != 0:
        raise ValidationError(
            f"Счёт «{account}» не проходит проверку контрольного разряда "
            f"для БИК {checked_bic}. Скорее всего, в номере опечатка."
        )
    return value


def _inn_control_digit(digits: tuple[int, ...], weights: tuple[int, ...]) -> int:
    """Считает одну контрольную цифру ИНН.

    Весов всегда меньше, чем разрядов: последние разряды — это сами
    контрольные цифры, в сумму они не входят. Поэтому ``strict=False``.
    """
    weighted = sum(
        weight * digit for weight, digit in zip(weights, digits, strict=False)
    )
    return weighted % INN_CHECKSUM_MODULUS % DECIMAL_MODULUS


def validate_inn(inn: str) -> str:
    """Проверяет ИНН юридического лица (10 цифр) или ИП и физлица (12 цифр).

    :raises ValidationError: если длина или контрольные цифры неверны.
    """
    value = inn.strip()
    if not value.isdigit() or len(value) not in INN_LENGTHS:
        lengths = " или ".join(str(length) for length in INN_LENGTHS)
        raise ValidationError(f"ИНН «{inn}» должен состоять из {lengths} цифр.")

    digits = tuple(int(char) for char in value)

    if len(value) == INN_COMPANY_LENGTH:
        if _inn_control_digit(digits, INN_COMPANY_WEIGHTS) != digits[-1]:
            raise ValidationError(
                f"ИНН «{inn}» не проходит проверку контрольной цифры."
            )
        return value

    first_ok = _inn_control_digit(digits, INN_PERSON_WEIGHTS_FIRST) == digits[-2]
    second_ok = _inn_control_digit(digits, INN_PERSON_WEIGHTS_SECOND) == digits[-1]
    if not (first_ok and second_ok):
        raise ValidationError(f"ИНН «{inn}» не проходит проверку контрольных цифр.")
    return value


def validate_kpp(kpp: str | None) -> str | None:
    """Проверяет КПП: девять цифр или пусто — у ИП его нет.

    :return: КПП или None, если он не задан.
    """
    if kpp is None or not kpp.strip():
        return None
    value = kpp.strip()
    if len(value) != KPP_LENGTH or not value.isdigit():
        raise ValidationError(
            f"КПП «{kpp}» должен состоять ровно из {KPP_LENGTH} цифр."
        )
    return value
