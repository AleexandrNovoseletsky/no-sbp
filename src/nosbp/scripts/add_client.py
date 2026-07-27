"""Утилита для добавления нового клиента сервиса.

Запуск внутри контейнера:
    docker compose exec qr-service python -m nosbp.scripts.add_client
"""

import asyncio
import secrets

from nosbp.db.database import async_session_maker, init_db
from nosbp.db.models import Client


async def main() -> None:
    await init_db()

    api_key = secrets.token_urlsafe(32)

    print("Добавление нового клиента. Реквизиты как в ГОСТ Р 56042-2014.\n")
    display_name = input("Название компании (для вас, для удобства в БД): ")
    name = input("Название получателя платежа (ФИО ИП / ООО): ")
    personal_acc = input("Расчётный счёт (20 цифр): ")
    bank_name = input("Банк получателя: ")
    bic = input("БИК (9 цифр): ")
    corresp_acc = input("Корр. счёт (20 цифр): ")
    payee_inn = input("ИНН получателя (10 или 12 цифр): ")

    client = Client(
        api_key=api_key,
        display_name=display_name,
        name=name,
        personal_acc=personal_acc,
        bank_name=bank_name,
        bic=bic,
        corresp_acc=corresp_acc,
        payee_inn=payee_inn,
    )

    async with async_session_maker() as session:
        session.add(client)
        await session.commit()

    print(f"\nГотово. api_key клиента:\n{api_key}\n")
    print("Сохраните его — из БД он не показывается в открытом виде нигде, кроме этого вывода.")


if __name__ == "__main__":
    asyncio.run(main())
