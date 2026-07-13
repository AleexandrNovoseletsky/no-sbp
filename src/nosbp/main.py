"""Точка взода в ASGI приложение."""

from fastapi import FastAPI

from nosbp.payments.routers import router as payment_qr_router

app = FastAPI(
    title="NoSBP API",
    description=("API для генерации QR-кода для оплаты "
                 "по счёту (ГОСТ 56042-2014)."
                 )
    )

app.include_router(
    router=payment_qr_router,
    prefix="/generate/qr",
    tags=["Payment QR Management"]
)


@app.get("/health", tags=["Health"])
async def healt_check():
    """Эндпоинт для проверки работоспособности API."""
    return {"status": "healthy", "service": "no-sbp-api"}

