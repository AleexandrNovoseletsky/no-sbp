"""Картинка-заглушка вместо ошибки.

QR-код живёт в теге ``<img>`` внутри уже отправленного письма. Если сервис
ответит кодом ошибки, получатель увидит битую иконку и не поймёт, что
делать, а заказчик об этом даже не узнает.

Поэтому на GET-запросах ошибка по умолчанию отдаётся картинкой с понятным
текстом. Машиночитаемый код при этом уезжает в заголовок ``X-NoSBP-Error``.
"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

AnyFont = ImageFont.FreeTypeFont | ImageFont.ImageFont

WIDTH: Final = 420
HEIGHT: Final = 420
"""Размер картинки в пикселях — примерно как QR-код при обычном масштабе,
чтобы вёрстка письма не поехала."""

BACKGROUND: Final[tuple[int, int, int]] = (255, 255, 255)
BORDER_COLOR: Final[tuple[int, int, int]] = (200, 205, 203)
TEXT_COLOR: Final[tuple[int, int, int]] = (120, 60, 40)

BORDER_INSET: Final = 6
"""Отступ рамки от края картинки."""

BORDER_WIDTH: Final = 2

FONT_SIZE: Final = 18
LINE_HEIGHT: Final = 26
TEXT_MARGIN: Final = 32
"""Отступ текста от боковых краёв. Ограничивает ширину строки при переносе."""

FONT_CACHE_SIZE: Final = 4

FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
"""Где искать шрифт с кириллицей.

Встроенный в Pillow растровый шрифт кириллицу не содержит, поэтому
в Dockerfile ставится пакет fonts-dejavu-core.
"""


@lru_cache(maxsize=FONT_CACHE_SIZE)
def _load_font(size: int) -> AnyFont:
    """Находит первый доступный шрифт с кириллицей.

    Результат кэшируется: разбор файла шрифта на каждой ошибке — заметная
    трата на пути, который и так возникает в неудачный момент.
    """
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Подходящего шрифта в системе нет: текст будет выведен встроенным
    # растровым шрифтом без поддержки кириллицы, но изображение
    # сформируется и вёрстку письма не нарушит.
    return ImageFont.load_default()


def _wrap(
    text: str, *, draw: ImageDraw.ImageDraw, font: AnyFont, max_width: int
) -> tuple[str, ...]:
    """Разбивает текст на строки, помещающиеся в заданную ширину.

    Перенос по реальной ширине текста, а не по числу символов: в кириллице
    и латинице символы разной ширины, и счёт по символам давал бы то
    обрезанные, то полупустые строки.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return tuple(lines)


def render_placeholder_png(message: str) -> bytes:
    """Рисует белый квадрат с рамкой и текстом ошибки.

    :param message: текст для человека, который смотрит на письмо.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (
            BORDER_INSET,
            BORDER_INSET,
            WIDTH - BORDER_INSET - 1,
            HEIGHT - BORDER_INSET - 1,
        ),
        outline=BORDER_COLOR,
        width=BORDER_WIDTH,
    )

    font = _load_font(FONT_SIZE)
    lines = _wrap(message, draw=draw, font=font, max_width=WIDTH - 2 * TEXT_MARGIN)

    # Обе величины постоянны для всех строк — считаем их до цикла.
    centre_x = WIDTH // 2
    start_y = (HEIGHT - len(lines) * LINE_HEIGHT) // 2

    for index, line in enumerate(lines):
        draw.text(
            (centre_x, start_y + index * LINE_HEIGHT),
            line,
            font=font,
            fill=TEXT_COLOR,
            anchor="ma",
        )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
