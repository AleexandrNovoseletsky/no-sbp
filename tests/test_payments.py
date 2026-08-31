import asyncio
from sqlalchemy import select

from src.nosbp.db.models import Client


def test_register_endpoint_creates_client_and_returns_api_key(client):
    # Arrange
    payload = {
        "display_name": "Acme",
        "name": "ООО Акме",
        "personal_acc": "12345678901234567890",
        "bank_name": "Банк",
        "bic": "012345678",
        "corresp_acc": "09876543210987654321",
        "payee_inn": "1234567890",
    }

    # Act
    resp = client.post("/generate/qr/register", json=payload)

    # Assert
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "api_key" in data and isinstance(data["api_key"], str)
    api_key = data["api_key"]

    # Verify client exists in DB
    async def _check():
        from src.nosbp.db.database import async_session_maker

        async with async_session_maker() as session:
            res = await session.execute(select(Client).where(Client.api_key == api_key))
            c = res.scalar_one_or_none()
            assert c is not None
            assert c.display_name == "Acme"

    asyncio.run(_check())


def test_post_generate_qr_returns_png(client, create_client):
    # Arrange: create client in DB
    api_key = create_client()

    # Act
    resp = client.post("/generate/qr/", json={"api_key": api_key, "payment_sum": 100})

    # Assert
    assert resp.status_code == 200, resp.text
    ct = resp.headers.get("content-type", "")
    assert "image/png" in ct
    assert resp.content.startswith(b"\x89PNG")


def test_get_generate_qr_returns_png(client, create_client):
    # Arrange
    api_key = create_client()

    # Act
    resp = client.get(f"/generate/qr/?api_key={api_key}&payment_sum=250&purpose=Test")

    # Assert
    assert resp.status_code == 200, resp.text
    ct = resp.headers.get("content-type", "")
    assert "image/png" in ct
    assert resp.content.startswith(b"\x89PNG")
