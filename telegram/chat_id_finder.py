"""
Утилита для получения списка групп и их Chat ID (ТЗ п.23).

Запуск:
    python -m telegram.chat_id_finder
"""
import asyncio

from telegram.client import build_client
from telethon.tl.types import Chat, Channel


async def main():
    client = build_client()
    await client.start()

    print("Group / Chat ID list:\n")
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Chat) or (isinstance(entity, Channel) and entity.megagroup):
            chat_type = "supergroup" if isinstance(entity, Channel) else "group"
            print(f"Group: {dialog.name}")
            print(f"Chat ID: {dialog.id}")
            print(f"Type: {chat_type}\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
