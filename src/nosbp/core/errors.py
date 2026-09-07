"""Доменные исключения сервиса.

Каждое исключение знает свой HTTP-статус и машиночитаемый код. Это позволяет
одному обработчику в ``main.py`` превращать любую доменную ошибку либо в JSON,
либо в картинку-заглушку — в зависимости от того, как клиент попросил.

Все доменные ошибки собраны здесь, а не по модулям: так видно весь набор
кодов, которые может получить клиент, в одном файле.
"""

from http import HTTPStatus


class NosbpError(Exception):
    """Базовая ошибка сервиса."""

    status_code: int = HTTPStatus.BAD_REQUEST
    code: str = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidTokenError(NosbpError):
    """Токен не найден или отозван."""

    status_code = HTTPStatus.UNAUTHORIZED
    code = "invalid_token"


class AccountDisabledError(NosbpError):
    """Аккаунт отключён оператором сервиса.

    Отдельно от нехватки денег: заказчику важно понимать, что дело
    не в балансе и пополнение ничего не изменит.
    """

    status_code = HTTPStatus.FORBIDDEN
    code = "account_disabled"


class OrganizationNotFoundError(NosbpError):
    """У аккаунта нет организации с таким alias (или нет организации вообще)."""

    status_code = HTTPStatus.NOT_FOUND
    code = "organization_not_found"


class InsufficientFundsError(NosbpError):
    """Баланс исчерпан и овердрафт закончился."""

    status_code = HTTPStatus.PAYMENT_REQUIRED
    code = "insufficient_funds"


class DailyLimitExceededError(NosbpError):
    """Превышен суточный потолок списаний по аккаунту."""

    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "daily_limit_exceeded"


class OverdraftNotActiveError(NosbpError):
    """Попытка продлить овердрафт, которого нет."""

    status_code = HTTPStatus.CONFLICT
    code = "overdraft_not_active"


class OverdraftExtensionLimitError(NosbpError):
    """Исчерпан лимит продлений овердрафта."""

    status_code = HTTPStatus.CONFLICT
    code = "overdraft_extension_limit"


class AdminAuthError(NosbpError):
    """Неверная пара «почта — пароль» или неверный одноразовый код.

    Текст намеренно один на все случаи: по разным сообщениям можно было бы
    выяснить, какие адреса заведены в системе.
    """

    status_code = HTTPStatus.UNAUTHORIZED
    code = "admin_auth_failed"


class AdminLockedError(NosbpError):
    """Вход временно заблокирован после неудачных попыток."""

    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "admin_locked"


class CabinetAuthError(NosbpError):
    """Неверная пара «почта — пароль» при входе в личный кабинет."""

    status_code = HTTPStatus.UNAUTHORIZED
    code = "cabinet_auth_failed"


class CabinetLockedError(NosbpError):
    """Вход в кабинет временно заблокирован после неудачных попыток."""

    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "cabinet_locked"


class EmailNotConfirmedError(NosbpError):
    """Адрес почты не подтверждён, а сервис настроен этого требовать.

    Отдельно от неверного пароля: учётные данные верны, и заказчику нужно
    объяснить, что делать, а не отправлять его подбирать пароль заново.
    """

    status_code = HTTPStatus.FORBIDDEN
    code = "email_not_confirmed"


class ValidationError(NosbpError):
    """Параметры запроса или сохраняемые данные не проходят проверку."""

    status_code = HTTPStatus.UNPROCESSABLE_CONTENT
    code = "validation_error"
