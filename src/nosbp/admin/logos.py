"""Загрузка логотипов организаций в объектное хранилище."""

import uuid
from collections.abc import Mapping
from io import BytesIO
from pathlib import PurePath
from typing import Final

from PIL import Image, UnidentifiedImageError

from nosbp.core.errors import ValidationError
from nosbp.storage.base import Storage

CONTENT_TYPES: Final[Mapping[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
"""Какие расширения принимаются и с каким типом кладутся в хранилище."""

KEY_TEMPLATE: Final[str] = "logos/{account_id}/{name}{suffix}"


def _validated_suffix(filename: str) -> str:
    """Проверяет расширение файла.

    :raises ValidationError: если формат не поддерживается.
    """
    suffix = PurePath(filename).suffix.lower()
    if suffix not in CONTENT_TYPES:
        supported = ", ".join(sorted(CONTENT_TYPES))
        raise ValidationError(f"Логотип должен быть в одном из форматов: {supported}.")
    return suffix


def _ensure_readable_image(data: bytes) -> None:
    """Убеждается, что байты действительно являются картинкой.

    Расширению верить нельзя: переименованный архив пройдёт проверку имени,
    а сломается уже при генерации QR — у заказчика в самый неподходящий
    момент.

    :raises ValidationError: если картинку не удалось прочитать.
    """
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError(
            "Файл не удалось прочитать как изображение. "
            "Проверьте, что это действительно PNG или JPEG."
        ) from exc


async def store_logo(
    *,
    storage: Storage,
    account_id: uuid.UUID,
    filename: str,
    data: bytes,
    max_bytes: int,
) -> str:
    """Проверяет и сохраняет логотип, возвращая ключ файла в хранилище.

    :raises ValidationError: если файл пуст, слишком велик, не той формы
        или не читается как изображение.
    """
    if not data:
        raise ValidationError("Файл логотипа пуст.")
    if len(data) > max_bytes:
        raise ValidationError(
            f"Логотип больше {max_bytes // 1024} КБ. Уменьшите картинку: "
            "в QR-коде он занимает меньше сотни пикселей."
        )

    suffix = _validated_suffix(filename)
    _ensure_readable_image(data)

    key = KEY_TEMPLATE.format(
        account_id=account_id, name=uuid.uuid4().hex, suffix=suffix
    )
    await storage.put(key, data, content_type=CONTENT_TYPES[suffix])
    return key
