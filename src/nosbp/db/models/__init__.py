"""Модели базы данных.

Схема отражает решения, зафиксированные при проектировании сервиса:

* учётная запись отделена от организаций-получателей платежа — у одного
  заказчика их может быть сколько угодно;
* ключи API и токены сессий хранятся только хэшем;
* персональные данные плательщика не хранятся: они участвуют лишь
  в вычислении ключа идемпотентности;
* баланс — это журнал операций, а не редактируемое поле.

Модели разнесены по модулям, но импортируются отсюда: SQLAlchemy
связывает их между собой при первом обращении, и все они должны быть
загружены до настройки отображений.
"""

from nosbp.db.models.accounts import (
    INVITE_PURPOSE_MAX_LENGTH,
    Account,
    AccountInvite,
    ApiToken,
    InvitePurpose,
    Organization,
)
from nosbp.db.models.admin import AdminUser
from nosbp.db.models.invoices import (
    ENTRY_TYPE_MAX_LENGTH,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
)
from nosbp.db.models.sessions import AccountSession, AdminSession, WebSession

__all__ = [
    "ENTRY_TYPE_MAX_LENGTH",
    "INVITE_PURPOSE_MAX_LENGTH",
    "Account",
    "AccountInvite",
    "AccountSession",
    "AdminSession",
    "AdminUser",
    "ApiToken",
    "InvitePurpose",
    "Invoice",
    "LedgerEntry",
    "LedgerEntryType",
    "Organization",
    "WebSession",
]
