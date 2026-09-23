"""Маршруты панели управления.

Разбиты по разделам интерфейса: вход, заказчики, организации, ключи.
"""

from fastapi import APIRouter

from nosbp.admin.routes import accounts, auth, organizations, tokens

router = APIRouter(tags=["admin"], include_in_schema=False)
router.include_router(auth.router)
router.include_router(accounts.router)
router.include_router(organizations.router)
router.include_router(tokens.router)

__all__ = ["router"]
