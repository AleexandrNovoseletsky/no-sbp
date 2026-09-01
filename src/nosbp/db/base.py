"""Базовый класс моделей и общие типы колонок."""

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

# Единое соглашение об именах индексов и ограничений.
# Без него Alembic придумывает имена сам, и они разъезжаются между средами —
# потом невозможно написать миграцию, которая удалит нужное ограничение.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime.datetime:
    """Текущее время в UTC с меткой часового пояса.

    Отдельная функция, а не ``datetime.now`` в аргументе по умолчанию:
    так её легко подменить в тестах и невозможно случайно вычислить время
    один раз при импорте модуля.
    """
    return datetime.datetime.now(datetime.UTC)


def uuid_pk() -> MappedColumn[Any]:
    """Первичный ключ — UUID, сгенерированный на стороне приложения.

    Генерируем в Python, а не в базе: так объект знает свой id ещё до
    коммита, и его можно использовать в связанных записях одной транзакции.
    """
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at_column() -> MappedColumn[Any]:
    """Колонка времени создания — всегда с часовым поясом."""
    return mapped_column(DateTime(timezone=True), default=utcnow, index=True)
