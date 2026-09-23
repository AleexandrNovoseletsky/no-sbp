"""Работа с денежными суммами.

Все суммы внутри сервиса — целые копейки. Числа с плавающей точкой для
денег не используются нигде: 0.1 + 0.2 не равно 0.3, и на балансе это
рано или поздно выльется в расхождение с журналом.

Рубли появляются только в двух местах: в тексте для человека и в аргументах
консольной утилиты. Оба перехода живут здесь, чтобы множитель 100 не был
рассыпан по коду.
"""

from decimal import Decimal, InvalidOperation

KOPECKS_PER_ROUBLE = 100
"""Копеек в рубле. Вынесено в константу, чтобы не искать «100» по коду."""


def to_roubles(kopecks: int) -> Decimal:
    """Переводит копейки в рубли точным десятичным числом."""
    return Decimal(kopecks) / KOPECKS_PER_ROUBLE


def to_kopecks(roubles: int) -> int:
    """Переводит целые рубли в копейки."""
    return roubles * KOPECKS_PER_ROUBLE


def format_roubles(kopecks: int, *, fractional: bool = True) -> str:
    """Готовит сумму для показа человеку.

    :param kopecks: сумма в копейках.
    :param fractional: показывать ли копейки. Для круглых сумм вроде
        минимального пополнения или суточного лимита они только мешают.
    """
    roubles = to_roubles(kopecks)
    return f"{roubles:.2f} ₽" if fractional else f"{roubles:.0f} ₽"


def roubles_input(kopecks: int | None) -> str:
    """Готовит сумму для поля ввода: «1000.00» или пустая строка.

    Отдельно от :func:`format_roubles`, потому что в поле не должно быть
    ни знака валюты, ни разделителей разрядов — иначе браузер вернёт
    строку, которую же сам и не разберёт.
    """
    if kopecks is None:
        return ""
    return f"{to_roubles(kopecks):.2f}"


def parse_roubles(text: str) -> int:
    """Разбирает введённую человеком сумму в рублях и переводит в копейки.

    Принимает и точку, и запятую: в русской раскладке на цифровом блоке
    запятая, и требовать точку — значит собирать жалобы на ровном месте.

    :raises ValueError: если строка не похожа на сумму.
    """
    cleaned = text.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not cleaned:
        raise ValueError("Сумма не указана.")
    try:
        roubles = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"«{text}» не похоже на сумму.") from exc

    kopecks = roubles * KOPECKS_PER_ROUBLE
    if kopecks != kopecks.to_integral_value():
        raise ValueError("Сумма указывается с точностью до копейки.")
    return int(kopecks)
