"""Отрисовка QR-кода в PNG.

Две особенности, из-за которых код сложнее одного вызова segno:

1. **Логотип в центре.** Он перекрывает часть модулей, поэтому при наличии
   логотипа поднимается уровень коррекции ошибок, а сам логотип
   ограничивается долей от ширины кода (:data:`LOGO_SIZE_RATIO`).
2. **Цвет.** Красятся только модули, фон всегда остаётся белым. Прозрачный
   фон выглядит аккуратнее в макете, но на тёмной подложке в письме
   делает код нечитаемым для сканера.
"""

from io import BytesIO
from typing import Final

import segno
from PIL import Image

from nosbp.core.constants import (
    DEFAULT_QR_BORDER,
    DEFAULT_QR_COLOR,
    DEFAULT_QR_SCALE,
)
from nosbp.core.errors import ValidationError

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

HEX_COLOR_DIGITS: Final = 6
"""Сколько шестнадцатеричных цифр в записи цвета ``RRGGBB``."""

CHANNEL_MAX: Final = 255

WHITE: Final[str] = "#ffffff"
WHITE_RGBA: Final[RGBA] = (CHANNEL_MAX, CHANNEL_MAX, CHANNEL_MAX, CHANNEL_MAX)

LOGO_SIZE_RATIO: Final = 7
"""Логотип занимает не больше 1/7 ширины кода по каждой стороне."""

LOGO_BACKDROP_PADDING: Final = 4
"""Ширина белой рамки вокруг логотипа в пикселях.

Отделяет логотип от модулей кода: без неё сканер принимает край логотипа
за модуль и ошибается.
"""

ERROR_LEVEL_WITH_LOGO: Final[str] = "h"
"""Уровень коррекции под логотипом: восстанавливается до 30 % данных."""

ERROR_LEVEL_PLAIN: Final[str] = "m"
"""Уровень коррекции без логотипа: 15 %, зато код заметно мельче."""

MIN_CONTRAST_RATIO: Final = 3.0
"""Минимальный контраст модулей к белому фону.

Порог WCAG для крупных элементов. Практически: чёрный, синий,
тёмно-зелёный и красный проходят; пастельные и светло-серые — нет,
и это правильно, потому что такие коды не сканируются.
"""

# Коэффициенты яркости каналов по WCAG 2.1 и параметры гамма-коррекции sRGB.
LUMINANCE_WEIGHTS: Final[tuple[float, float, float]] = (0.2126, 0.7152, 0.0722)
SRGB_LINEAR_THRESHOLD: Final = 0.04045
SRGB_LINEAR_DIVISOR: Final = 12.92
SRGB_GAMMA_OFFSET: Final = 0.055
SRGB_GAMMA_EXPONENT: Final = 2.4
CONTRAST_OFFSET: Final = 0.05
"""Слагаемое из формулы контраста WCAG: (L1 + 0.05) / (L2 + 0.05)."""


def parse_hex_color(value: str) -> RGB:
    """Разбирает цвет вида ``#RRGGBB`` или ``RRGGBB`` в тройку каналов.

    :raises ValidationError: если строка не похожа на шестнадцатеричный цвет.
    """
    raw = value.strip().removeprefix("#")
    if len(raw) != HEX_COLOR_DIGITS:
        raise ValidationError(
            f"Цвет «{value}» должен быть в формате #RRGGBB, например #0E6B62."
        )
    try:
        red, green, blue = (int(raw[start : start + 2], 16) for start in (0, 2, 4))
    except ValueError as exc:
        raise ValidationError(
            f"Цвет «{value}» не является шестнадцатеричным числом."
        ) from exc
    return red, green, blue


def _to_linear(channel: int) -> float:
    """Снимает гамма-коррекцию sRGB с одного канала."""
    srgb = channel / CHANNEL_MAX
    if srgb <= SRGB_LINEAR_THRESHOLD:
        return srgb / SRGB_LINEAR_DIVISOR
    # float ** float в typeshed объявлен как Any, поэтому приводим явно.
    normalized = (srgb + SRGB_GAMMA_OFFSET) / (1 + SRGB_GAMMA_OFFSET)
    return float(normalized**SRGB_GAMMA_EXPONENT)


def relative_luminance(rgb: RGB) -> float:
    """Относительная яркость цвета по формуле WCAG 2.1."""
    return sum(
        weight * _to_linear(channel)
        for weight, channel in zip(LUMINANCE_WEIGHTS, rgb, strict=True)
    )


def contrast_with_white(rgb: RGB) -> float:
    """Контраст цвета к белому фону: от 1 (белый) до 21 (чёрный)."""
    return (1 + CONTRAST_OFFSET) / (relative_luminance(rgb) + CONTRAST_OFFSET)


def validate_qr_color(value: str) -> str:
    """Проверяет, что этим цветом можно нарисовать сканируемый QR-код.

    :return: нормализованный цвет в формате ``#rrggbb``.
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
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def render_qr_png(
    payload: str,
    *,
    logo: bytes | None = None,
    color: str = DEFAULT_QR_COLOR,
    scale: int = DEFAULT_QR_SCALE,
    border: int = DEFAULT_QR_BORDER,
) -> bytes:
    """Рисует QR-код и возвращает его как PNG.

    :param payload: строка, которая кодируется (обычно результат
        :func:`~nosbp.payments.gost.build_gost_payload`).
    :param logo: PNG или JPEG логотипа для вставки в центр, либо None.
    :param color: цвет модулей в формате ``#RRGGBB``.
    :param scale: сколько пикселей занимает один модуль.
    :param border: ширина белого поля вокруг кода в модулях.
    :raises ValidationError: если цвет не проходит проверку контраста.
    """
    dark = validate_qr_color(color)
    has_logo = logo is not None
    error_level = ERROR_LEVEL_WITH_LOGO if has_logo else ERROR_LEVEL_PLAIN

    qr = segno.make_qr(content=payload, error=error_level)
    buffer = BytesIO()
    qr.save(out=buffer, kind="png", scale=scale, border=border, dark=dark, light=WHITE)

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
    # поэтому маленький логотип не растянется в пиксельную кашу.
    size_limit = qr_image.width // LOGO_SIZE_RATIO
    logo_image.thumbnail((size_limit, size_limit))

    # Белая подложка под логотипом: полупрозрачный логотип иначе пропустит
    # сквозь себя модули кода, и сканер об них споткнётся.
    backdrop = Image.new(
        "RGBA",
        (
            logo_image.width + 2 * LOGO_BACKDROP_PADDING,
            logo_image.height + 2 * LOGO_BACKDROP_PADDING,
        ),
        WHITE_RGBA,
    )
    qr_image.alpha_composite(backdrop, _centre_offset(qr_image, backdrop))
    qr_image.alpha_composite(logo_image, _centre_offset(qr_image, logo_image))

    result = BytesIO()
    qr_image.save(result, format="PNG")
    return result.getvalue()


def _centre_offset(canvas: Image.Image, overlay: Image.Image) -> tuple[int, int]:
    """Считает точку, в которой ``overlay`` окажется по центру ``canvas``."""
    return (
        (canvas.width - overlay.width) // 2,
        (canvas.height - overlay.height) // 2,
    )
