"""Конфигурация логирования на базе structlog."""

import logging
import sys

import structlog


def configure_logging(*, json_logs: bool, log_level: str = "INFO") -> None:
    """Настраивает structlog.

    :param json_logs: если True — логи в формате JSON (для прода/сбора логов),
        если False — цветной человекочитаемый вывод (для локальной разработки).
    :param log_level: минимальный уровень логирования (DEBUG, INFO, WARNING...).
    """
    # Процессоры, которые отрабатывают ВСЕГДА, независимо от формата вывода.
    # Каждый из них получает словарь event_dict и что-то в него добавляет
    # или трансформирует.
    shared_processors: list[structlog.typing.Processor] = [
        # Подмешивает contextvars
        structlog.contextvars.merge_contextvars,
        # Добавляет уровень лога в event_dict: {"level": "info", ...}
        structlog.processors.add_log_level,
        # Добавляет таймстамп в формате ISO 8601
        structlog.processors.TimeStamper(fmt="iso"),
        # Если логируется исключение (log.exception(...)) —
        # красиво форматирует traceback
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Позволяет использовать %-style форматирование в event, как в stdlib logging
        structlog.stdlib.PositionalArgumentsFormatter(),
    ]

    if json_logs:
        # Прод: одна строка JSON на запись. Легко парсится системами сбора логов.
        renderer = structlog.processors.JSONRenderer()
    else:
        # Локально: цветной, с отступами, читаемый глазами вывод.
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            # Этот процессор должен идти последним перед рендерером —
            # он готовит event_dict к передаче в stdlib logging.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # Используем стандартный logging как "движок" вывода
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ProcessorFormatter — мост между structlog и stdlib logging.
    # Он нужен, чтобы через эту же систему проходили и логи от сторонних
    # библиотек (uvicorn, fastapi, sqlalchemy и т.д.), которые пишут
    # через обычный stdlib logging и ничего не знают про structlog.
    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain применяется ТОЛЬКО к записям от сторонних библиотек,
        # чтобы привести их к тому же виду, что и наши structlog-записи.
        foreign_pre_chain=shared_processors,
        processors=[
            # Убирает служебные ключи, которые нужны были только для передачи
            # между structlog и stdlib logging
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Приглушаем слишком болтливые сторонние логгеры
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
