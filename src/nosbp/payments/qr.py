from io import BytesIO

import segno
from PIL import Image


def render_qr_png(payload: str, logo: bytes | None) -> bytes:
    """Преобразует строку в PNG файл."""
    
    error_level: str = "M"
    if logo is not None:
        eroor_level = "H"
    qr = segno.make_qr(content=payload, error=error_level)
    buffer = BytesIO()
    qr.save(out=buffer, kind="png", scale=10, border=4)

    if logo is None:
        return buffer.getvalue()

    # Вставляет логотип в центр картинки
    qr_image = Image.open(buffer).convert("RGBA")
    logo_image = Image.open(BytesIO(logo)).convert("RGBA")

    logo_size = qr_image.width // 7
    logo_image.thumbnail((logo_size, logo_size))

    position = (
        (qr_image.width - logo_image.width) // 2,
        (qr_image.height - logo_image.height) // 2,
    )
    qr_image.alpha_composite(logo_image, position)

    result = BytesIO()
    qr_image.save(result, format="PNG")

    return result.getvalue()

