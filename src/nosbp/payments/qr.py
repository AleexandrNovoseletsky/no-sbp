from io import BytesIO

import segno


def render_qr_png(payload: str) -> bytes:
    """Преобразует строку в PNG файл."""

    qr = segno.make_qr(content=payload)
    buffer = BytesIO()
    qr.save(out=buffer, kind="png", scale=10, border=4)
    return buffer.getvalue()
