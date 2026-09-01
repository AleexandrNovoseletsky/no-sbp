"""Картинка-заглушка вместо ошибки.

QR-код живёт в теге ``<img>`` внутри уже отправленного письма. Если сервис
ответит кодом 402, получатель увидит битую иконку и не поймёт, что делать,
а заказчик об этом даже не узнает.

Поэтому на GET-запросах ошибка по умолчанию отдаётся картинкой с понятным
текстом. Машиночитаемый код при этом уезжает в заголовок ``X-NoSBP-Error``.
"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 420
HEIGHT = 420
BACKGROUND = (255, 255, 255)
BORDER_COLOR = (200, 205, 203)
TEXT_COLOR = (120, 60, 40)

FONT_CANDIDATES = (
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


@lru_cache(maxsize=4)
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Находит первый доступный шрифт с кириллицей."""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    # Крайний случай: шрифта в системе нет. Текст будет латиницей,
    # но картинка всё равно отрисуется и не сломает вёрстку письма.
    return ImageFont.load_default()


def _wrap(text: str, limit: int) -> list[str]:
    """Разбивает текст на строки не длиннее ``limit`` символов."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render_placeholder_png(message: str) -> bytes:
    """Рисует белый квадрат с рамкой и текстом ошибки.

    :param message: текст для человека, который смотрит на письмо.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, WIDTH - 7, HEIGHT - 7), outline=BORDER_COLOR, width=2)

    font = _load_font(18)
    lines = _wrap(message, limit=32)

    line_height = 26
    start_y = (HEIGHT - len(lines) * line_height) // 2
    for index, line in enumerate(lines):
        draw.text(
            (WIDTH // 2, start_y + index * line_height),
            line,
            font=font,
            fill=TEXT_COLOR,
            anchor="ma",
        )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
