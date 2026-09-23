"""mail based password recovery

Восстановление пароля по почте: назначение одноразовой ссылки и отметка
о подтверждении адреса.

Revision ID: a1a35d55724b
Revises: ebe64dff28b0
Create Date: 2026-09-07 16:58:35.003371
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1a35d55724b"
down_revision: str | None = "ebe64dff28b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSE_TYPE = sa.Enum(
    "PASSWORD",
    "EMAIL",
    name="invitepurpose",
    native_enum=False,
    length=16,
)

DEFAULT_PURPOSE = "PASSWORD"
"""Все ссылки, выданные до этой миграции, вели на установку пароля."""


def upgrade() -> None:
    op.add_column(
        "account_invites",
        # server_default нужен только на время добавления колонки:
        # без него NOT NULL не проходит на непустой таблице. Сразу после
        # заполнения он снимается, чтобы значение задавало приложение.
        sa.Column(
            "purpose",
            PURPOSE_TYPE,
            nullable=False,
            server_default=DEFAULT_PURPOSE,
        ),
    )
    op.alter_column("account_invites", "purpose", server_default=None)
    op.add_column(
        "accounts",
        sa.Column("email_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("accounts", "email_confirmed_at")
    op.drop_column("account_invites", "purpose")
