"""Тесты отрисовки QR-кода: цвет, логотип, читаемость."""

from io import BytesIO

import pytest
import segno
from PIL import Image

from nosbp.core.errors import ValidationError
from nosbp.payments.qr import (
    contrast_with_white,
    parse_hex_color,
    render_qr_png,
    validate_qr_color,
)
from tests.qrdecode import decode_qr

PAYLOAD = "ST00012|Name=ООО Ромашка|PersonalAcc=40702810000000000007"

ASCII_PAYLOAD = (
    "ST00012|Name=OOO Romashka|PersonalAcc=40702810000000000007|"
    "BankName=PAO Sberbank|BIC=044525225|CorrespAcc=30101810000000000007|"
    "PayeeINN=7707083893|Sum=4700000"
)
"""Строка без кириллицы — на ней проверяется точное совпадение.

OpenCV искажает байты вне ASCII, поэтому побайтовое сравнение возможно
только на латинице. Кодирование кириллицы — забота segno, а не нашего кода.
"""

CYRILLIC_PAYLOAD = (
    "ST00012|Name=ООО Ромашка|PersonalAcc=40702810000000000007|"
    "BankName=ПАО Сбербанк|BIC=044525225|CorrespAcc=30101810000000000007|"
    "PayeeINN=7707083893|KPP=773601001|Purpose=Оплата заказа 1234|Sum=4700000"
)
"""Реалистичная строка: в UTF-8 кириллица занимает по два байта, поэтому
код получается заметно крупнее — именно такие и нужно проверять."""


def make_logo(
    size: int = 200, color: tuple[int, int, int, int] = (200, 30, 30, 255)
) -> bytes:
    """Рисует простой квадратный логотип."""
    image = Image.new("RGBA", (size, size), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Цвет
# ---------------------------------------------------------------------------


def test_hex_color_is_parsed_with_and_without_hash():
    assert parse_hex_color("#0E6B62") == (14, 107, 98)
    assert parse_hex_color("0e6b62") == (14, 107, 98)


@pytest.mark.parametrize("value", ["#12345", "#GGGGGG", "синий"])
def test_malformed_color_is_rejected(value: str):
    with pytest.raises(ValidationError):
        parse_hex_color(value)


def test_black_has_maximum_contrast():
    assert contrast_with_white((0, 0, 0)) == pytest.approx(21.0, abs=0.1)


def test_dark_brand_colors_are_accepted():
    for color in ("#000000", "#0E6B62", "#1F3A93", "#B22222"):
        assert validate_qr_color(color) == color.lower()


@pytest.mark.parametrize("value", ["#FFFFFF", "#F0F0F0", "#FFD700", "#AEEEEE"])
def test_light_colors_are_rejected_as_unscannable(value: str):
    """Светлый QR не сканируется — лучше отказать заказчику сразу."""
    with pytest.raises(ValidationError, match="светлый"):
        validate_qr_color(value)


def test_color_is_normalised_to_lowercase_with_hash():
    assert validate_qr_color("0E6B62") == "#0e6b62"


# ---------------------------------------------------------------------------
# Отрисовка
# ---------------------------------------------------------------------------


def test_render_returns_png():
    png = render_qr_png(PAYLOAD)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_rendered_qr_is_decodable():
    assert decode_qr(render_qr_png(ASCII_PAYLOAD)) == ASCII_PAYLOAD


def test_color_actually_reaches_the_image():
    png = render_qr_png(PAYLOAD, color="#b22222")
    with Image.open(BytesIO(png)) as image:
        colors = {
            color for _, color in image.convert("RGB").getcolors(maxcolors=100000)
        }
    assert (178, 34, 34) in colors


def test_background_stays_white():
    """Прозрачный фон на тёмной подложке письма делает код нечитаемым."""
    png = render_qr_png(PAYLOAD, color="#0e6b62")
    with Image.open(BytesIO(png)) as image:
        corner = image.convert("RGB").getpixel((2, 2))
    assert corner == (255, 255, 255)


def test_logo_is_placed_in_the_centre():
    png = render_qr_png(PAYLOAD, logo=make_logo())
    with Image.open(BytesIO(png)) as image:
        rgb = image.convert("RGB")
        centre = rgb.getpixel((rgb.width // 2, rgb.height // 2))
    assert centre == (200, 30, 30)


def test_logo_raises_error_correction_level():
    """Под логотипом часть модулей не читается, поэтому нужен уровень H.

    Более высокая избыточность требует более крупной матрицы — по размеру
    картинки и видно, что уровень действительно поднялся.
    """
    plain = render_qr_png(PAYLOAD)
    with_logo = render_qr_png(PAYLOAD, logo=make_logo())

    with Image.open(BytesIO(plain)) as image:
        plain_width = image.width
    with Image.open(BytesIO(with_logo)) as image:
        logo_width = image.width

    assert logo_width > plain_width
    assert (
        segno.make_qr(PAYLOAD, error="h").version
        > segno.make_qr(PAYLOAD, error="m").version
    )


def test_qr_with_logo_is_still_decodable():
    """Главная проверка модуля: логотип в центре не должен ломать код."""
    png = render_qr_png(ASCII_PAYLOAD, logo=make_logo())
    assert decode_qr(png) == ASCII_PAYLOAD


def test_coloured_qr_with_logo_is_decodable():
    """Обе возможности разом — цвет и логотип."""
    png = render_qr_png(ASCII_PAYLOAD, logo=make_logo(), color="#0e6b62")
    assert decode_qr(png) == ASCII_PAYLOAD


def test_long_cyrillic_payload_with_logo_is_decodable():
    """Самый тяжёлый реальный случай: длинная кириллица, логотип и цвет.

    Такая строка кодируется 15-й версией QR — самой крупной из тех,
    что встречаются в работе. Если сканируется она, сканируется всё.
    """
    png = render_qr_png(CYRILLIC_PAYLOAD, logo=make_logo(), color="#0e6b62")
    decoded = decode_qr(png)

    assert decoded is not None, "код не распознан вовсе"
    # Кириллицу OpenCV искажает, а вот ASCII-части обязаны совпасть.
    assert "PersonalAcc=40702810000000000007" in decoded
    assert decoded.endswith("Sum=4700000")


def test_oversized_logo_is_scaled_down():
    """Огромный логотип обязан ужаться, а не перекрыть весь код."""
    logo_color = (200, 30, 30)
    png = render_qr_png(PAYLOAD, logo=make_logo(size=4000))

    with Image.open(BytesIO(png)) as image:
        rgb = image.convert("RGB")
        centre_row = rgb.height // 2
        centre = rgb.getpixel((rgb.width // 2, centre_row))
        quarter = rgb.getpixel((rgb.width // 4, centre_row))

    # В центре логотип есть, а на четверти ширины его быть уже не может:
    # предел — седьмая часть ширины, то есть примерно 0.43…0.57 по X.
    assert centre == logo_color
    assert quarter != logo_color


def test_broken_logo_bytes_give_clear_error():
    with pytest.raises(ValidationError, match="PNG или JPEG"):
        render_qr_png(PAYLOAD, logo=b"not an image at all")
