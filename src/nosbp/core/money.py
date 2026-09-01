"""Работа с денежными суммами.

Все суммы внутри сервиса — целые копейки. Числа с плавающей точкой для
денег не используются нигде: 0.1 + 0.2 не равно 0.3, и на балансе это
рано или поздно выльется в расхождение с журналом.

Рубли появляются только в двух местах: в тексте для человека и в аргументах
консольной утилиты. Оба перехода живут здесь, чтобы множитель 100 не был
рассыпан по коду.
"""

from decimal import Decimal

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
