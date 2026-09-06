"""Маршруты личного кабинета заказчика.

Разбиты по разделам интерфейса: вход, обзор и отчёты, организации, ключи.
Каждый раздел — отдельный модуль со своим набором эндпоинтов.
"""

from fastapi import APIRouter

from nosbp.cabinet.routes import auth, organizations, overview, tokens

router = APIRouter(tags=["cabinet"], include_in_schema=False)
router.include_router(auth.router)
router.include_router(overview.router)
router.include_router(organizations.router)
router.include_router(tokens.router)

__all__ = ["router"]
