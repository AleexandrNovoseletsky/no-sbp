"""Отрисовка QR-кода в PNG.

Две особенности, из-за которых код сложнее одного вызова segno:

1. **Логотип в центре.** Он перекрывает часть модулей, поэтому при наличии
   логотипа поднимается уровень коррекции ошибок до «H» (восстанавливается
   до 30 % данных), а размер логотипа ограничивается седьмой частью
   ширины кода.
2. **Цвет.** Красятся только модули, фон всегда остаётся белым. Прозрачный
   фон выглядит аккуратнее в макете, но на тёмной подложке в письме
   делает код нечитаемым для сканера.
"""

from io import BytesIO

import segno
from PIL import Image

from nosbp.core.errors import ValidationError

LOGO_SIZE_RATIO = 7
"""Логотип занимает не больше 1/7 ширины кода по каждой стороне."""

MIN_CONTRAST_RATIO = 3.0
"""Минимальный контраст модулей к белому фону.

Значение по WCAG для крупных элементов. Практически: чёрный, синий,
тёмно-зелёный, красный проходят; пастельные и светло-серые — нет,
и это правильно, потому что такие коды не сканируются.
"""


def parse_hex_color(value: str) -> tuple[int, int, int]:
    """Разбирает цвет вида ``#RRGGBB`` или ``RRGGBB`` в тройку каналов.

    :raises ValidationError: если строка не похожа на шестнадцатеричный цвет.
    """
    raw = value.strip().lstrip("#")
    if len(raw) != 6:
        raise ValidationError(
            f"Цвет «{value}» должен быть в формате #RRGGBB, например #0E6B62."
        )
    try:
        return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValidationError(
            f"Цвет «{value}» не является шестнадцатеричным числом."
        ) from exc


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """Относительная яркость цвета по формуле WCAG 2.1."""

    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_with_white(rgb: tuple[int, int, int]) -> float:
    """Контраст цвета к белому фону: от 1 (белый) до 21 (чёрный)."""
    return 1.05 / (relative_luminance(rgb) + 0.05)


def validate_qr_color(value: str) -> str:
    """Проверяет, что цветом можно рисовать сканируемый QR-код.

    :return: нормализованный цвет в формате ``#RRGGBB`` в нижнем регистре.
    :raises ValidationError: если цвет слишком светлый.
    """
    rgb = parse_hex_color(value)
    contrast = contrast_with_white(rgb)
    if contrast < MIN_CONTRAST_RATIO:
        raise ValidationError(
            f"Цвет «{value}» слишком светлый: контраст к белому фону "
            f"{contrast:.1f} при минимуме {MIN_CONTRAST_RATIO}. "
            "Такой QR-код не будет сканироваться. Возьмите цвет потемнее."
        )
    return "#" + "".join(f"{c:02x}" for c in rgb)


def render_qr_png(
    payload: str,
    *,
    logo: bytes | None = None,
    color: str = "#000000",
    scale: int = 10,
    border: int = 4,
) -> bytes:
    """Рисует QR-код и возвращает его как PNG.

    :param payload: строка, которая кодируется (обычно результат
        :func:`~nosbp.payments.gost.build_gost_payload`).
    :param logo: PNG или JPEG логотипа для вставки в центр, либо None.
    :param color: цвет модулей в формате ``#RRGGBB``.
    :param scale: сколько пикселей занимает один модуль.
    :param border: ширина белого поля вокруг кода в модулях. Меньше 4
        не рекомендуется: сканеры используют это поле для поиска кода.
    :raises ValidationError: если цвет не проходит проверку контраста.
    """
    dark = validate_qr_color(color)

    # Под логотипом часть модулей не читается, поэтому нужна максимальная
    # избыточность. Без логотипа хватает уровня «M» — код получается мельче.
    error_level = "h" if logo is not None else "m"

    qr = segno.make_qr(content=payload, error=error_level)
    buffer = BytesIO()
    qr.save(
        out=buffer, kind="png", scale=scale, border=border, dark=dark, light="#ffffff"
    )

    if logo is None:
        return buffer.getvalue()

    return _overlay_logo(buffer.getvalue(), logo)


def _overlay_logo(qr_png: bytes, logo: bytes) -> bytes:
    """Накладывает логотип на центр готового QR-кода."""
    with Image.open(BytesIO(qr_png)) as qr_source:
        qr_image = qr_source.convert("RGBA")

    try:
        with Image.open(BytesIO(logo)) as logo_source:
            logo_image = logo_source.convert("RGBA")
    except OSError as exc:
        raise ValidationError(
            "Логотип не удалось прочитать: ожидается PNG или JPEG."
        ) from exc

    # thumbnail сохраняет пропорции и никогда не увеличивает картинку,
    # поэтому маленький логотип не будет растянут в пиксельную кашу.
    limit = qr_image.width // LOGO_SIZE_RATIO
    logo_image.thumbnail((limit, limit))

    # Белая подложка под логотипом: если логотип полупрозрачный, модули
    # кода будут просвечивать сквозь него и мешать распознаванию.
    backdrop_size = (logo_image.width + 8, logo_image.height + 8)
    backdrop = Image.new("RGBA", backdrop_size, (255, 255, 255, 255))
    backdrop_position = (
        (qr_image.width - backdrop.width) // 2,
        (qr_image.height - backdrop.height) // 2,
    )
    qr_image.alpha_composite(backdrop, backdrop_position)

    logo_position = (
        (qr_image.width - logo_image.width) // 2,
        (qr_image.height - logo_image.height) // 2,
    )
    qr_image.alpha_composite(logo_image, logo_position)

    result = BytesIO()
    qr_image.save(result, format="PNG")
    return result.getvalue()
