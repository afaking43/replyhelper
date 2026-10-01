"""
Подключение личного Telegram-аккаунта через Telethon (MTProto).
"""
import asyncio
import logging
import os

from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError

from config import config

logger = logging.getLogger("telegram")


def build_client() -> TelegramClient:
    return TelegramClient(
        config.telegram_session_name,
        config.telegram_api_id,
        config.telegram_api_hash,
    )


def _session_paths() -> list[str]:
    name = config.telegram_session_name
    return [
        f"{name}.session",
        f"{name}.session-journal",
    ]


async def wipe_session(client: TelegramClient) -> None:
    """Сбрасывает битый session-файл (ключ больше не зарегистрирован в Telegram)."""
    logger.warning("Resetting invalid Telegram session")
    try:
        if client.is_connected():
            await client.disconnect()
    except Exception:
        pass
    try:
        client.session.delete()
    except Exception:
        pass
    for path in _session_paths():
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Removed session file: %s", path)
        except OSError as exc:
            logger.error("Cannot remove %s: %s", path, exc)
    await client.connect()


async def connect_client(client: TelegramClient) -> bool:
    """
    Подключается без интерактивного логина.
    Возвращает True, если аккаунт уже авторизован.
    """
    if not client.is_connected():
        await client.connect()

    try:
        authorized = await client.is_user_authorized()
    except AuthKeyUnregisteredError:
        await wipe_session(client)
        return False

    if not authorized:
        logger.warning(
            "Telegram account is not authorized. Use the admin bot (🔑 Авторизация) to log in."
        )
        return False

    try:
        me = await client.get_me()
    except AuthKeyUnregisteredError:
        await wipe_session(client)
        return False

    logger.info(
        "Telegram account connected: %s (id=%s)",
        me.username or me.first_name,
        me.id,
    )
    return True


async def run_client_forever(client: TelegramClient) -> None:
    """
    Держит Telethon живым. Битая сессия не роняет админ-бота.
    После авторизации через бота цикл сам подхватит updates.
    """
    while True:
        try:
            if not client.is_connected():
                await client.connect()

            try:
                authorized = await client.is_user_authorized()
            except AuthKeyUnregisteredError:
                await wipe_session(client)
                await asyncio.sleep(3)
                continue

            if not authorized:
                await asyncio.sleep(3)
                continue

            logger.info("Telethon update loop started")
            await client.run_until_disconnected()
            logger.warning("Telethon disconnected, reconnecting...")
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except AuthKeyUnregisteredError:
            logger.warning("Auth key unregistered, session will be reset")
            try:
                await wipe_session(client)
            except Exception:
                logger.exception("Failed to wipe session")
            await asyncio.sleep(3)
        except Exception:
            logger.exception("Telethon loop error, retrying")
            await asyncio.sleep(5)
