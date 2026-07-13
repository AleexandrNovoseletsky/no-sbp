
from fastapi import FastAPI

app = FastAPI(
    title="NoSBP API",
    description=("API для генерации QR-кода для оплаты "
                 "по счёту (ГОСТ 56042-2014)."
                 )
        )


@app.get("/health", tags=["Health"])
async def healt_check():
    """Эндпоинт для проверки работоспособности API."""
    return {"status": "healthy", "service": "no-sbp-api"}

