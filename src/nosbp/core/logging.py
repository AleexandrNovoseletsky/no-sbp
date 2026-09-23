"""Конфигурация логирования на базе structlog."""

import logging
import sys

import structlog


def configure_logging(*, json_logs: bool, log_level: str = "INFO") -> None:
    """Настраивает structlog.

    :param json_logs: если True — логи в формате JSON (для прода и систем
        сбора логов), если False — цветной человекочитаемый вывод.
    :param log_level: минимальный уровень логирования (DEBUG, INFO, ...).
    """
    # Процессоры, которые отрабатывают всегда, независимо от формата вывода.
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.PositionalArgumentsFormatter(),
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Готовит event_dict к передаче в stdlib logging — должен идти
            # последним перед рендерером.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Мост между structlog и stdlib logging: нужен, чтобы логи сторонних
    # библиотек (uvicorn, sqlalchemy) выглядели так же, как наши.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Переконфигурация не должна плодить обработчики (важно для тестов).
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
