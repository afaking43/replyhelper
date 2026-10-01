"""
Обработчик входящих сообщений (многопользовательский режим).

Каждый чат принадлежит одному пользователю (владелцу).
Владелец определяется по watchlist: чат ищется в списках всех активированных
пользователей. Данные (шаблоны, приветствия, благодарности, исключения, задачи)
берутся именно у владельца чата.

Правила фильтрации:
- только чаты из watchlist какого-либо пользователя;
- игнорировать каналы и неизвестные чаты;
- игнорировать собственные сообщения (event.out);
- игнорировать сообщения от ботов;
- игнорировать уже обработанные сообщения.

При сообщении клиента:
1. Личный аккаунт отвечает по шаблону (или текстом по умолчанию).
   Голое приветствие получает ответ-приветствие вместо шаблона и в задачи не попадает.
   Голая благодарность — робот молчит (или отправляет «ответ на благодарность»).
2. Задача сохраняется владельцу чата; ему же приходит уведомление.
3. Если владелец сам ответил клиенту с личного аккаунта, робот молчит 5 минут.
"""
import asyncio
import html
import logging
import re
import time

from aiogram import Bot
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from config import config
from storage.context import ContextStore
from storage.settings import settings_store

logger = logging.getLogger("telegram")

_pending_texts: dict[tuple[int, int], list[str]] = {}
_pending_tasks: dict[tuple[int, int], asyncio.Task] = {}
_pending_owners: dict[tuple[int, int], int] = {}

_HUMAN_PAUSE_SECONDS = 5 * 60
_robot_sent: dict[tuple[int, int, int], float] = {}
_sending_chat_keys: set[tuple[int, int]] = set()
_human_active: dict[tuple[int, int], float] = {}
_last_greeted: dict[tuple[int, int], float] = {}

_NOTIFY_DEBOUNCE_SECONDS = 10
_pending_user_notify: dict[int, list[int]] = {}
_notify_user_generations: dict[int, int] = {}


def _schedule_user_notify(user_id: int, task_id: int) -> None:
    _pending_user_notify.setdefault(user_id, []).append(task_id)
    gen = _notify_user_generations.get(user_id, 0) + 1
    _notify_user_generations[user_id] = gen
    asyncio.create_task(_send_user_notify(user_id, gen))


async def _send_user_notify(user_id: int, generation: int) -> None:
    await asyncio.sleep(_NOTIFY_DEBOUNCE_SECONDS)
    if generation != _notify_user_generations.get(user_id):
        return
    ids = _pending_user_notify.pop(user_id, [])
    if not ids:
        return
    if len(ids) == 1:
        text = f"🔔 <b>Вам поступил запрос!</b>\nЗадача {ids[0]} — откройте меню 📌 Задачи."
    else:
        text = (
            "🔔 <b>Вам поступили запросы!</b>\n"
            f"Задачи {', '.join(str(i) for i in ids)} — откройте меню 📌 Задачи."
        )
    try:
        bot_token = settings_store.get_bot_token() or config.admin_bot_token
        if not bot_token:
            return
        bot = Bot(token=bot_token)
        await bot.send_message(user_id, text, parse_mode="HTML")
        await bot.session.close()
    except Exception as exc:
        logger.error("Failed to send notification to user %s: %s", user_id, exc)

_chat_owner_map: dict[int, int] = {}

_PUNCT_RE = re.compile(r"[^\w\s]")


def _plain(text: str) -> str:
    value = _PUNCT_RE.sub(" ", (text or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", value).strip()


def rebuild_chat_owner_map() -> None:
    """Перестроить карту chat_id -> user_id по watchlist всех пользователей."""
    global _chat_owner_map
    _chat_owner_map.clear()
    for uid in settings_store.get_all_user_ids():
        for cid_str in settings_store.get_user_watchlist(uid):
            try:
                cid = int(cid_str)
            except ValueError:
                continue
            if cid not in _chat_owner_map:
                _chat_owner_map[cid] = uid
    logger.info(
        "[Telegram] Owner map rebuilt: %d chats across %d users",
        len(_chat_owner_map),
        len(settings_store.get_all_user_ids()),
    )


def _owner_of(chat_id: int) -> int | None:
    return _chat_owner_map.get(chat_id)


def _is_watched_peer(chat) -> bool:
    if chat is None:
        return False
    if isinstance(chat, Chat):
        return True
    if isinstance(chat, Channel) and getattr(chat, "megagroup", False):
        return True
    if isinstance(chat, User) and not getattr(chat, "bot", False):
        return True
    return False


def _chat_meta(chat, sender, chat_id) -> tuple[str, str, str]:
    sender_name = (
        getattr(sender, "first_name", None)
        or getattr(sender, "username", None)
        or "Unknown"
    )
    if isinstance(chat, User):
        full_name = " ".join(
            part for part in [chat.first_name, chat.last_name] if part
        )
        title = full_name or chat.username or sender_name or str(chat_id)
        return title, sender_name, "client"
    title = getattr(chat, "title", None) or str(chat_id)
    return title, sender_name, "group"


def format_task_html(task: dict) -> str:
    kind_label = "Клиент" if task.get("kind") == "client" else "Группа"
    status_label = "✅ задача сделана" if task.get("status") == "done" else "Отправлен текст"
    request = html.escape(task.get("request") or "")
    reply = html.escape(task.get("reply") or "")
    title = html.escape(task.get("chat_title") or "")
    client = html.escape(task.get("client") or "")
    return (
        f"📌 <b>Задача {task.get('id')} ({status_label})</b>\n\n"
        f"<b>{kind_label}:</b> {title}\n"
        f"<b>Клиент:</b> {client}\n\n"
        f"<b>Запрос:</b>\n{request}\n\n"
        f"<b>Робот:</b>\n{reply}"
    )


def register_handlers(client: TelegramClient, store: ContextStore, user_id: int | None = None) -> None:
    rebuild_chat_owner_map()

    @client.on(events.NewMessage())
    async def on_new_message(event):
        try:
            await _handle(event, client, store, user_id)
        except Exception as e:
            logger.exception("Unhandled error while processing message")
            settings_store.record_event("error")
            settings_store.add_error_log(
                level="error",
                module="telegram.handler",
                message=str(e),
                details=logger.handlers and str(e) or "",
            )

    async def _handle(event, client: TelegramClient, store: ContextStore, client_user_id: int | None = None):
        chat_id = event.chat_id

        owner_id = client_user_id if client_user_id is not None else _owner_of(chat_id)
        if owner_id is None:
            return

        if chat_id not in settings_store.get_user_allowed_chat_ids(owner_id):
            return

        chat = await event.get_chat()
        if not _is_watched_peer(chat):
            return

        state_key = (owner_id, chat_id)

        if event.out:
            is_robot = state_key in _sending_chat_keys or _robot_sent.pop(
                (owner_id, chat_id, event.id), None
            ) is not None
            if not is_robot:
                _human_active[state_key] = time.time()
                settings_store.record_event("manual_reply", user_id=owner_id)
                logger.info(
                    "[Telegram] Manual reply detected for owner %s — robot paused for %s min",
                    owner_id,
                    _HUMAN_PAUSE_SECONDS // 60,
                )
            return

        sender = await event.get_sender()
        if sender is not None and getattr(sender, "bot", False):
            return

        if sender is not None:
            username = getattr(sender, "username", "") or ""
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            name = f"{first} {last}".strip() or username or f"User {sender.id}"
            settings_store.add_subscriber(
                telegram_id=sender.id,
                name=name,
                username=username,
                source="telegram",
            )

        text = (event.raw_text or "").strip()
        if not text:
            return

        if store.is_processed(chat_id, event.id, user_id=owner_id):
            return
        store.mark_processed(chat_id, event.id, user_id=owner_id)

        if not settings_store.assistant_enabled or not config.auto_reply:
            return

        if not settings_store.get_user_assistant_enabled(owner_id):
            return

        if sender is not None and settings_store.is_user_excluded_for(owner_id, sender.id):
            logger.info("[Telegram] User %s excluded for owner %s — skipping", sender.id, owner_id)
            return

        if settings_store.is_no_action_phrase_for(owner_id, text):
            logger.info("[Telegram] No-action phrase detected for owner %s — skipping", owner_id)
            return

        chat_title, sender_name, kind = _chat_meta(chat, sender, chat_id)
        logger.info("[Telegram] New message (owner=%s)", owner_id)
        logger.info("[Telegram] Chat: %s", chat_title)
        logger.info("[Telegram] User: %s", sender_name)
        logger.info("[Telegram] Message: %s", text)

        settings_store.record_event("incoming", user_id=owner_id)

        await _buffer_and_schedule(
            chat_id, text, event, client, chat_title, sender_name, kind, owner_id
        )

    async def _buffer_and_schedule(
        chat_id, text, event, client, chat_title, sender_name, kind, owner_id
    ):
        state_key = (owner_id, chat_id)
        _pending_texts.setdefault(state_key, []).append(text)
        _pending_owners[state_key] = owner_id

        existing = _pending_tasks.get(state_key)
        if existing and not existing.done():
            existing.cancel()

        _pending_tasks[state_key] = asyncio.create_task(
            _flush_after_delay(
                chat_id, event, client, chat_title, sender_name, kind, owner_id
            )
        )

    async def _flush_after_delay(
        chat_id, event, client: TelegramClient, chat_title: str,
        sender_name: str, kind: str, owner_id: int
    ):
        state_key = (owner_id, chat_id)
        try:
            await asyncio.sleep(config.batch_window)
        except asyncio.CancelledError:
            return

        texts = _pending_texts.pop(state_key, [])
        _pending_owners.pop(state_key, None)
        if not texts:
            return
        combined_text = "\n".join(texts)

        if not settings_store.assistant_enabled or not config.auto_reply:
            logger.info("[Telegram] Assistant disabled — no reply, no task")
            return

        if not settings_store.get_user_assistant_enabled(owner_id):
            logger.info("[Telegram] User assistant disabled — no reply, no task")
            return

        human_ts = _human_active.get(state_key)
        if human_ts is not None and (time.time() - human_ts) < _HUMAN_PAUSE_SECONDS:
            logger.info(
                "[Telegram] Human takeover active for owner %s — robot silent, no task created",
                owner_id,
            )
            return

        if settings_store.is_user_thanks(owner_id, combined_text):
            thanks_reply = (settings_store.get_user_thanks_reply(owner_id) or "").strip()
            if not thanks_reply:
                logger.info("[Telegram] Thanks-only message — no reaction")
                return
            logger.info("[Telegram] Thanks detected — sending thanks reply")
            await asyncio.sleep(config.reply_delay)
            _sending_chat_keys.add(state_key)
            try:
                sent = await client.send_message(chat_id, thanks_reply, reply_to=event.id)
            finally:
                _sending_chat_keys.discard(state_key)
            now = time.time()
            for key in [k for k, ts in _robot_sent.items() if now - ts > 900]:
                _robot_sent.pop(key, None)
            _robot_sent[(owner_id, chat_id, sent.id)] = now
            logger.info("[Telegram] Thanks reply sent")
            return

        greeting = settings_store.user_greeting_reply(owner_id, combined_text)
        now = time.time()
        if greeting:
            last_greeted = _last_greeted.get(state_key, 0)
            if now - last_greeted < _HUMAN_PAUSE_SECONDS:
                logger.info("[Telegram] Chat already greeted recently — skipping")
                greeting = None
        reply_text = greeting if greeting else settings_store.find_user_reply(owner_id, combined_text)

        logger.info("[Telegram] Sending scenario response...")
        await asyncio.sleep(config.reply_delay)
        _sending_chat_keys.add(state_key)
        try:
            sent = await client.send_message(chat_id, reply_text, reply_to=event.id)
            settings_store.record_event("auto_reply", user_id=owner_id)
        finally:
            _sending_chat_keys.discard(state_key)
        now = time.time()
        for key in [k for k, ts in _robot_sent.items() if now - ts > 900]:
            _robot_sent.pop(key, None)
        _robot_sent[(owner_id, chat_id, sent.id)] = now
        logger.info("[Telegram] Response sent")

        if greeting:
            _last_greeted[state_key] = now
            return

        task = settings_store.add_user_task(
            owner_id,
            chat_id=chat_id,
            chat_title=chat_title,
            client=sender_name,
            kind=kind,
            request=combined_text,
            reply=reply_text,
        )

        if config.admin_bot_token or settings_store.get_bot_token():
            _schedule_user_notify(owner_id, task["id"])
