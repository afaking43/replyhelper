"""
Точка входа. Запускает одновременно:
1. Личный Telegram-аккаунт (Telethon userbot) для прослушивания групп и автоответов.
2. Административного бота (aiogram Bot API) для управления настройками и получения задач.
3. Веб-админ-панель (FastAPI + uvicorn) для управления через браузер.
"""
import asyncio
import logging
import os
import sys

import uvicorn
from aiogram import Bot
from telethon import TelegramClient

from config import config
from telegram.client import build_client, connect_client, run_client_forever
from telegram.handler import register_handlers
from storage.context import ContextStore
from storage.settings import settings_store
from admin_bot.bot import start_admin_bot
from web_admin.app import set_telethon_getter


def setup_logging() -> None:
    import io
    stdout_handler = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer")
        else sys.stdout
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s: %(message)s",
        handlers=[
            stdout_handler,
            logging.FileHandler("logs/app.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def start_web_admin() -> None:
    """Запустить веб-админ-панель."""
    port = int(os.getenv("PORT", config.web_port))
    web_config = uvicorn.Config(
        "web_admin.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(web_config)
    await server.serve()


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")

    config.validate()

    # ──────────────────────────────────────────────────────────────────────
    # Multi-user Telethon: один клиент на каждого активированного пользователя
    # ──────────────────────────────────────────────────────────────────────
    user_clients: dict[int, TelegramClient] = {}
    activated_creds = settings_store.get_activated_credentials()

    for cred in activated_creds:
        uid = int(cred["user_id"])
        api_id = cred.get("api_id")
        api_hash = cred.get("api_hash")
        if not api_id or not api_hash:
            logger.warning("User %s: missing api_id/api_hash — skipping", uid)
            continue
        session_path = f"session_{uid}.session"
        if not os.path.exists(session_path):
            logger.warning(
                "User %s: session file '%s' not found — skipping (authorize via admin bot)",
                uid, session_path,
            )
            continue
        try:
            client = TelegramClient(
                f"session_{uid}",
                api_id,
                api_hash,
                device_model="Desktop",
                system_version="Windows 10",
                app_version="4.16.8 x64",
                lang_code="ru",
                system_lang_code="ru",
            )
            await connect_client(client)
            user_clients[uid] = client
            logger.info("User %s: Telethon client connected", uid)
        except Exception as exc:
            logger.warning("User %s: failed to connect — %s", uid, exc)

    # Fallback: глобальный клиент из .env (для обратной совместимости)
    global_telethon_client = None
    if config.telegram_api_id and config.telegram_api_hash and not user_clients:
        global_telethon_client = build_client()
        try:
            await connect_client(global_telethon_client)
        except Exception as exc:
            logger.warning("Telethon initial connect warning: %s", exc)

    if not user_clients and not global_telethon_client:
        logger.warning("Telethon credentials not set — skipping userbot")

    # Регистрируем обработчики на все активные клиенты
    store = ContextStore(window=config.context_window)
    primary_client = None
    for uid, client in user_clients.items():
        register_handlers(client, store, user_id=uid)
        if primary_client is None:
            primary_client = client
    if global_telethon_client:
        register_handlers(global_telethon_client, store)
        primary_client = global_telethon_client

    def get_client(user_id: int | None = None):
        if user_id is not None and user_id in user_clients:
            return user_clients[user_id]
        return primary_client

    logger.info("Application started in Scenario Mode.")
    logger.info("Auto Reply Scenario text: %s", settings_store.scenario_reply)
    total_chats = sum(
        len(settings_store.get_user_watchlist(uid))
        for uid in settings_store.get_all_user_ids()
    )
    logger.info("Total watched chats across all users: %s", total_chats)
    logger.info("Active Telethon accounts: %s", len(user_clients))
    logger.info("Web admin panel: http://0.0.0.0:%s", config.web_port)
    logger.info("ADMIN_USER_ID from DB: %s", config.admin_user_id)

    bot_token = settings_store.get_bot_token() or config.admin_bot_token
    logger.info("Bot token source: settings_store=%s, config=%s",
                bool(settings_store.get_bot_token()), bool(config.admin_bot_token))

    if bot_token:
        try:
            notifier = Bot(token=bot_token)
            if config.admin_user_id:
                state_label = (
                    "🟢 работает"
                    if settings_store.assistant_enabled
                    else "🔴 выключен"
                )
                await notifier.send_message(
                    config.admin_user_id,
                    "🚀 <b>Проект запущен</b>\n"
                    f"Помощник: {state_label}\n"
                    f"Активных аккаунтов: {len(user_clients)}\n"
                    f"Пользователей: {len(settings_store.get_activated_credentials())}",
                    parse_mode="HTML",
                )
            await notifier.session.close()
        except Exception as exc:
            logger.warning("Failed to send startup notification: %s", exc)
    else:
        logger.error("=" * 60)
        logger.error("BOT TOKEN: NOT SET — Admin bot will NOT start")
        logger.error("Set ADMIN_BOT_TOKEN in Railway Variables")
        logger.error("=" * 60)

    set_telethon_getter(get_client)

    tasks = [start_web_admin()]
    logger.info("Task started: web_admin (uvicorn)")

    # Запускаем loop для каждого пользовательского клиента
    for uid, client in user_clients.items():
        tasks.append(run_client_forever(client))
        logger.info("Task started: Telethon client for user %s", uid)

    if global_telethon_client:
        tasks.append(run_client_forever(global_telethon_client))
        logger.info("Task started: global Telethon client")

    if bot_token:
        tasks.append(start_admin_bot(get_client))
        logger.info("Task started: admin_bot (aiogram)")

    logger.info("=" * 60)
    logger.info("Total async tasks: %d", len(tasks))
    logger.info("=" * 60)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Application stopped.")
