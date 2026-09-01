"""Доменные исключения сервиса.

Каждое исключение знает свой HTTP-статус и машиночитаемый код. Это позволяет
одному обработчику в ``main.py`` превращать любую доменную ошибку либо в JSON,
либо в картинку-заглушку — в зависимости от того, как клиент попросил.
"""


class NosbpError(Exception):
    """Базовая ошибка сервиса."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidTokenError(NosbpError):
    """Токен не найден, отозван или принадлежит отключённому аккаунту."""

    status_code = 401
    code = "invalid_token"


class OrganizationNotFoundError(NosbpError):
    """У аккаунта нет организации с таким alias (или нет организации вообще)."""

    status_code = 404
    code = "organization_not_found"


class InsufficientFundsError(NosbpError):
    """Баланс исчерпан и овердрафт закончился."""

    status_code = 402
    code = "insufficient_funds"


class DailyLimitExceededError(NosbpError):
    """Превышен суточный потолок списаний по аккаунту."""

    status_code = 429
    code = "daily_limit_exceeded"


class ValidationError(NosbpError):
    """Параметры запроса не проходят проверку."""

    status_code = 422
    code = "validation_error"
