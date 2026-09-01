"""Проверка банковских реквизитов.

Все эти проверки выполняются один раз — когда заказчик сохраняет
организацию в кабинете. Смысл в том, чтобы поймать опечатку в момент
ввода, а не тогда, когда деньги уже ушли на несуществующий счёт.

На горячем пути генерации QR ничего из этого не вызывается.
"""

from nosbp.core.errors import ValidationError

CORRESPONDENT_ACCOUNT_PREFIX = "30101"
"""С этого начинаются корреспондентские счета — у них другой алгоритм."""

CHECKSUM_WEIGHTS = (7, 1, 3)
"""Веса разрядов при расчёте контрольной суммы счёта."""


def validate_bic(bic: str) -> str:
    """Проверяет БИК: девять цифр.

    :return: БИК без изменений, если он корректен.
    :raises ValidationError: если формат нарушен.
    """
    value = bic.strip()
    if len(value) != 9 or not value.isdigit():
        raise ValidationError(f"БИК «{bic}» должен состоять ровно из 9 цифр.")
    return value


def validate_account(account: str, bic: str) -> str:
    """Проверяет расчётный или корреспондентский счёт по контрольному разряду.

    Алгоритм ЦБ РФ: к счёту слева приписывается трёхзначный префикс,
    зависящий от типа счёта, затем считается взвешенная сумма разрядов.
    Если она не делится на 10 — в счёте опечатка.

    :param account: номер счёта, 20 цифр.
    :param bic: БИК банка, в котором открыт счёт.
    :raises ValidationError: если формат или контрольная сумма не сходятся.
    """
    value = account.strip()
    if len(value) != 20 or not value.isdigit():
        raise ValidationError(f"Счёт «{account}» должен состоять ровно из 20 цифр.")

    bic = validate_bic(bic)

    if value.startswith(CORRESPONDENT_ACCOUNT_PREFIX):
        # Для корсчёта берутся 5-й и 6-й разряды БИК.
        prefix = "0" + bic[4:6]
    else:
        # Для расчётного счёта — условный номер подразделения банка,
        # то есть последние три разряда БИК.
        prefix = bic[6:9]

    digits = prefix + value
    checksum = sum(
        int(digit) * CHECKSUM_WEIGHTS[index % len(CHECKSUM_WEIGHTS)]
        for index, digit in enumerate(digits)
    )
    if checksum % 10 != 0:
        raise ValidationError(
            f"Счёт «{account}» не проходит проверку контрольного разряда "
            f"для БИК {bic}. Скорее всего, в номере опечатка."
        )
    return value


def validate_inn(inn: str) -> str:
    """Проверяет ИНН юридического лица (10 цифр) или ИП/физлица (12 цифр).

    :raises ValidationError: если длина или контрольные цифры неверны.
    """
    value = inn.strip()
    if not value.isdigit() or len(value) not in (10, 12):
        raise ValidationError(f"ИНН «{inn}» должен состоять из 10 или 12 цифр.")

    digits = [int(char) for char in value]

    def control(weights: tuple[int, ...]) -> int:
        # У 12-значного ИНН весов меньше, чем цифр: последние разряды —
        # это сами контрольные цифры, в сумму они не входят.
        weighted = sum(
            weight * digit for weight, digit in zip(weights, digits, strict=False)
        )
        return weighted % 11 % 10

    if len(value) == 10:
        if control((2, 4, 10, 3, 5, 9, 4, 6, 8)) != digits[9]:
            raise ValidationError(
                f"ИНН «{inn}» не проходит проверку контрольной цифры."
            )
    else:
        first_ok = control((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[10]
        second_ok = control((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[11]
        if not (first_ok and second_ok):
            raise ValidationError(f"ИНН «{inn}» не проходит проверку контрольных цифр.")
    return value


def validate_kpp(kpp: str | None) -> str | None:
    """Проверяет КПП: девять цифр или пусто (у ИП КПП нет)."""
    if kpp is None or not kpp.strip():
        return None
    value = kpp.strip()
    if len(value) != 9 or not value.isdigit():
        raise ValidationError(f"КПП «{kpp}» должен состоять ровно из 9 цифр.")
    return value
