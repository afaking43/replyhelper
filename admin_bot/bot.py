"""
Административный бот (aiogram 3):
многопользовательская система с изоляцией данных.
"""
import asyncio
import html
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from telethon.tl.types import Channel, Chat, User
from telethon.tl.types.auth import (
    SentCodeTypeApp,
    SentCodeTypeCall,
    SentCodeTypeFlashCall,
    SentCodeTypeSms,
)

from config import config
from storage.settings import settings_store
from telegram.handler import format_task_html, rebuild_chat_owner_map

logger = logging.getLogger("admin_bot")

router = Router()
PAGE_SIZE = 10
_dialog_cache: dict[int, list[tuple[int, str, str]]] = {}   # user_id -> dialogs
_selection: dict[int, set[int]] = {}                         # user_id -> selected chat_ids
_contact_cache: dict[int, list[tuple[int, str, str]]] = {}  # user_id -> contacts
_chats_filter: dict[int, str] = {}                           # user_id -> filter
_add_filter: dict[int, str] = {}                             # user_id -> filter


def _get_dialog_cache(uid: int) -> list[tuple[int, str, str]]:
    return _dialog_cache.get(uid, [])

def _get_selection(uid: int) -> set[int]:
    if uid not in _selection:
        _selection[uid] = set()
    return _selection[uid]

def _get_contact_cache(uid: int) -> list[tuple[int, str, str]]:
    return _contact_cache.get(uid, [])

def _get_chats_filter(uid: int) -> str:
    return _chats_filter.get(uid, "all")

def _get_add_filter(uid: int) -> str:
    return _add_filter.get(uid, "all")
_pending_auth_clients: dict[int, TelegramClient] = {}
_user_clients: dict[int, TelegramClient] = {}


def _clean_user_session(uid: int) -> None:
    """Удаляет временные или повреждённые session-файлы пользователя."""
    for ext in (".session", ".session-journal"):
        path = f"session_{uid}{ext}"
        if os.path.exists(path):
            try:
                os.remove(path)
                logger.info("Removed session file: %s", path)
            except OSError as exc:
                logger.warning("Cannot remove %s: %s", path, exc)


async def _get_client_for_user(uid: int) -> TelegramClient | None:
    if hasattr(router, "telethon_client_getter") and router.telethon_client_getter:
        try:
            user_client = router.telethon_client_getter(uid)
            if user_client:
                if not user_client.is_connected():
                    await user_client.connect()
                if await user_client.is_user_authorized():
                    return user_client
        except Exception:
            pass

    if uid in _user_clients and _user_clients[uid].is_connected():
        return _user_clients[uid]

    creds = settings_store.get_user_credentials(uid)
    if not creds or not creds.get("activated"):
        return None

    api_id = creds.get("api_id")
    api_hash = creds.get("api_hash")
    if not api_id or not api_hash:
        return None

    session_path = f"session_{uid}.session"
    if not os.path.exists(session_path):
        return None

    try:
        user_client = TelegramClient(
            f"session_{uid}",
            api_id,
            api_hash,
            device_model="Desktop",
            system_version="Windows 10",
            app_version="4.16.8 x64",
            lang_code="ru",
            system_lang_code="ru",
        )
        await user_client.connect()
        if await user_client.is_user_authorized():
            _user_clients[uid] = user_client
            return user_client
    except Exception as exc:
        logger.warning("Failed to connect user client for %s: %s", uid, exc)
    return None


async def _safe_edit_or_answer(bot: Bot, chat_id: int, message_id: int | None, text: str, reply_markup=None):
    if message_id:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")


class AuthStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()


class SettingsStates(StatesGroup):
    waiting_for_scenario_reply = State()
    waiting_for_question = State()
    waiting_for_answer = State()
    waiting_for_greet_phrase = State()
    waiting_for_greet_reply = State()
    waiting_for_thanks = State()
    waiting_for_thanks_reply = State()
    waiting_for_lib_title = State()
    waiting_for_lib_text = State()
    waiting_for_no_action_phrase = State()
    waiting_for_user_creds = State()
    waiting_for_sms_code = State()
    waiting_for_password = State()
    waiting_for_std_greet_phrase = State()
    waiting_for_std_greet_reply = State()
    waiting_for_std_thanks = State()
    waiting_for_std_no_action = State()


def _is_admin(user_id: int | None) -> bool:
    return True


def _short(text: str, limit: int = 40) -> str:
    value = (text or "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _assistant_on() -> bool:
    return bool(config.auto_reply and settings_store.assistant_enabled)


def _assistant_on_for_user(user_id: int) -> bool:
    return _assistant_on() and settings_store.get_user_assistant_enabled(user_id)


def _user_is_activated(user_id: int) -> bool:
    creds = settings_store.get_user_credentials(user_id)
    return bool(creds and creds.get("activated"))


def _assistant_keyboard_for_user(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if config.web_app_base_url:
        rows.append([InlineKeyboardButton(
            text="📋 Шаблоны",
            web_app=WebAppInfo(url=f"{config.web_app_base_url}/webapp/{user_id}?uid={user_id}"),
        )])
    rows.extend([
        [InlineKeyboardButton(text="📋 Группы и клиенты", callback_data="menu_chats")],
        [InlineKeyboardButton(text="🚫 Исключения", callback_data="menu_excluded")],
        [InlineKeyboardButton(text="👋 Приветствия и благодарности", callback_data="menu_words")],
        [InlineKeyboardButton(text="💬 Сообщения и ответы", callback_data="menu_messages")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_user_settings")],
    ])
    if user_id == config.admin_user_id:
        rows.append([InlineKeyboardButton(text="🛡 Админ-панель", callback_data="menu_admin_panel")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    open_tasks = len(settings_store.get_user_tasks(user_id, done=False))
    tasks_label = f"📌 Задачи ({open_tasks})" if open_tasks else "📌 Задачи"
    global_on = _assistant_on()
    if not global_on:
        status_text = "🔴 Помощник выключен администратором"
    else:
        user_on = settings_store.get_user_assistant_enabled(user_id)
        status_text = "🟢 Статус: работает" if user_on else "🔴 Статус: выключен"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Справочник", callback_data="menu_lib")],
            [InlineKeyboardButton(text=tasks_label, callback_data="menu_tasks")],
            [InlineKeyboardButton(text="🤖 Помощник", callback_data="menu_assistant")],
            [
                InlineKeyboardButton(
                    text=status_text,
                    callback_data="toggle_assistant",
                )
            ],
        ]
    )


def _home_text(user_id: int | None = None) -> str:
    default_reply = html.escape(settings_store.scenario_reply)
    if not _assistant_on():
        status = "🔴 Выключен администратором"
    elif user_id and not settings_store.get_user_assistant_enabled(user_id):
        status = "🔴 Выключен"
    else:
        status = "🟢 Работает"
    return (
        "🤖 <b>Панель управления</b>\n\n"
        f"Статус: {status}\n"
        f"Ответ по умолчанию:\n<i>«{default_reply}»</i>\n\n"
        "📚 Справочник — ваши тексты для клиентов\n"
        "📌 Задачи — открытые задачи (внутри раздел «Сделанные»)\n"
        "🤖 Помощник — настройки автоответа\n\n"
        "Кнопка «Статус» включает и выключает вашего помощника"
    )


def _kind_label(chat_type: str) -> str:
    return "клиент" if chat_type == "client" else "группа"


def get_back_keyboard(callback_data: str = "menu_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
        ]
    )


def _page_slice(items: list, page: int) -> tuple[list, int, int]:
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    return chunk, page, total_pages


def _nav_rows(prefix: str, page: int, total_pages: int) -> list[list[InlineKeyboardButton]]:
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}{page + 1}"))
    return [nav] if nav else []


def _assistant_text(user_id: int | None = None) -> str:
    status = "🟢 Работает" if _assistant_on() else "🔴 Выключен"
    if user_id and user_id != config.admin_user_id and not _user_is_activated(user_id):
        return (
            "🤖 <b>Помощник</b>\n\n"
            f"Статус: {status}\n\n"
            "Для использования помощника нужно пройти настройку.\n"
            "Нажмите «🔑 Настройка помощника» и отправьте данные вашего аккаунта."
        )
    return (
        "🤖 <b>Помощник</b>\n\n"
        f"Статус: {status}\n\n"
        "📋 Группы и клиенты — куда включён автоответ\n"
        "🚫 Исключения — люди, на чьи сообщения бот не отвечает\n"
        "👋 Приветствия и благодарности — слова робота\n"
        "💬 Сообщения и ответы — варианты вопросов и ответы\n"
        "⚙️ Настройки — информация о вашем аккаунте"
    )


def chats_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    cf = _get_chats_filter(user_id)
    items = list(settings_store.get_user_watchlist(user_id).items())
    if cf == "groups":
        items = [(c, i) for c, i in items if i.get("type") == "group"]
    elif cf == "contacts":
        items = [(c, i) for c, i in items if i.get("type") == "client"]
    chunk, page, total_pages = _page_slice(items, page)
    rows: list[list[InlineKeyboardButton]] = []
    g_mark = "🔘" if cf == "groups" else "⚪️"
    c_mark = "🔘" if cf == "contacts" else "⚪️"
    rows.append([
        InlineKeyboardButton(text=f"{g_mark} Группы", callback_data="chats_filter_groups"),
        InlineKeyboardButton(text=f"{c_mark} Контакты", callback_data="chats_filter_contacts"),
    ])
    for cid, info in chunk:
        title = _short(info.get("title", ""), 32)
        kind = _kind_label(info.get("type", "group"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"❌ {title} ({kind})",
                    callback_data=f"del_chat_{cid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить", callback_data="menu_add_chat")]
    )
    rows.extend(_nav_rows("chat_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _chats_text(user_id: int, page: int = 0) -> str:
    cf = _get_chats_filter(user_id)
    chats = settings_store.get_user_watchlist(user_id)
    if not chats:
        return (
            "📋 <b>Группы и клиенты</b>\n\n"
            "Список пуст. Автоответ никуда не ходит.\n"
            "Нажмите «Добавить», выберите группу или клиента из Telegram."
        )
    items = list(chats.items())
    if cf == "groups":
        items = [(c, i) for c, i in items if i.get("type") == "group"]
    elif cf == "contacts":
        items = [(c, i) for c, i in items if i.get("type") == "client"]
    chunk, page, total_pages = _page_slice(items, page)
    lines = [f"📋 <b>Автоответ включён здесь — {page + 1}/{total_pages}:</b>\n"]
    for cid, info in chunk:
        title = html.escape(info.get("title", ""))
        kind = _kind_label(info.get("type", "group"))
        lines.append(f"• {title} <i>({kind})</i>")
    lines.append("\nНажмите ❌ чтобы убрать чат из списка.")
    return "\n".join(lines)


def tasks_header_keyboard(user_id: int) -> InlineKeyboardMarkup:
    done_count = len(settings_store.get_user_tasks(user_id, done=True))
    done_label = f"🗂 Сделанные ({done_count})" if done_count else "🗂 Сделанные"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=done_label, callback_data="menu_tasks_done")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="menu_home")],
        ]
    )


def done_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Очистить память", callback_data="tasks_clear_done")],
            [InlineKeyboardButton(text="◀️ К задачам", callback_data="menu_tasks")],
        ]
    )


def _done_text(user_id: int) -> str:
    count = len(settings_store.get_user_tasks(user_id, done=True))
    if not count:
        return "🗂 <b>Сделанные</b>\n\nСписок пуст."
    return (
        "🗂 <b>Сделанные задачи</b>\n\n"
        f"Всего: {count}.\n"
        "Сейчас пришлю каждую отдельным сообщением — удаляйте кнопкой 🗑."
    )


def messages_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    templates = settings_store.get_user_templates(user_id)
    chunk, page, total_pages = _page_slice(templates, page)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить вопрос и ответ", callback_data="tpl_add")],
        [InlineKeyboardButton(text="✍️ Ответ по умолчанию", callback_data="menu_edit_reply")],
        [InlineKeyboardButton(text="🗑 Очистить все шаблоны", callback_data="tpl_clear_all")],
    ]
    for item in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {_short(item.get('question', ''), 28)}",
                    callback_data=f"del_tpl_{item['id']}",
                )
            ]
        )
    rows.extend(_nav_rows("tpl_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _messages_text(user_id: int, page: int = 0) -> str:
    default_reply = html.escape(settings_store.scenario_reply)
    lines = [
        "💬 <b>Сообщения и ответы</b>\n",
        "Если в тексте клиента есть вариант вопроса — робот отправит связанный ответ.",
        "Если совпадения нет — уйдёт ответ по умолчанию.\n",
        f"<b>По умолчанию:</b>\n<i>«{default_reply}»</i>\n",
    ]
    templates = settings_store.get_user_templates(user_id)
    if not templates:
        lines.append("Шаблонов пока нет. Добавьте пару «вопрос → ответ».")
        return "\n".join(lines)
    chunk, page, total_pages = _page_slice(templates, page)
    lines.append(f"<b>Шаблоны — {page + 1}/{total_pages}:</b>")
    for item in chunk:
        question = html.escape(item.get("question", ""))
        answer = html.escape(item.get("answer", ""))
        lines.append(f"\n<b>{item.get('id')}.</b> Вопрос: <i>«{question}»</i>")
        lines.append(f"Ответ: <i>«{answer}»</i>")
    return "\n".join(lines)


def words_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Start msg", callback_data="menu_start_msg")],
            [InlineKeyboardButton(text="🙏 End msg", callback_data="menu_end_msg")],
            [InlineKeyboardButton(text="⏸ Без ответа", callback_data="menu_no_action")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")],
        ]
    )


def _words_text() -> str:
    return (
        "👋 <b>Приветствия и благодарности</b>\n\n"
        "<b>Start msg</b> — клиент прислал голое приветствие,\n"
        "робот отвечает ответом-приветствием.\n\n"
        "<b>End msg</b> — клиент прислал голую благодарность,\n"
        "робот молчит (нет ответа и задачи).\n\n"
        "<b>⏸ Без ответа</b> — фразы, на которые бот не реагирует.\n\n"
        "Выберите раздел:"
    )


def start_msg_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    user_items = settings_store.get_user_greetings(user_id)
    std_items = settings_store.get_standard_greetings()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    chunk, page, total_pages = _page_slice(all_items, page)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="greet_add")]
    ]
    for is_std, item in chunk:
        phrase = _short(item.get("phrase", ""), 18)
        reply = _short(item.get("reply", ""), 22)
        if is_std:
            rows.append(
                [InlineKeyboardButton(text=f"📌 {phrase} → {reply}", callback_data="noop")]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 {phrase} → {reply}",
                        callback_data=f"del_greet_{item['id']}",
                    )
                ]
            )
    rows.extend(_nav_rows("greet_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_words")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _start_msg_text(user_id: int, page: int = 0) -> str:
    user_items = settings_store.get_user_greetings(user_id)
    std_items = settings_store.get_standard_greetings()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    chunk, page, total_pages = _page_slice(all_items, page)
    lines = [
        "🟢 <b>Start msg</b>\n",
        "Клиент прислал голое приветствие → робот отвечает ответом-приветствием.\n",
        "📌 — стандартные (нельзя удалить).\n",
        f"<b>Слова (фраза → ответ) — {page + 1}/{total_pages}:</b>",
    ]
    if chunk:
        for is_std, item in chunk:
            prefix = "📌" if is_std else "•"
            lines.append(
                f"{prefix} «{html.escape(item.get('phrase', ''))}» → "
                f"<i>«{html.escape(item.get('reply', ''))}»</i>"
            )
    else:
        lines.append("<i>пока пусто</i>")
    lines.append("\nНажмите 🗑 чтобы удалить слово, ➕ чтобы добавить.")
    return "\n".join(lines)


def end_msg_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    user_items = settings_store.get_user_thanks(user_id)
    std_items = settings_store.get_standard_thanks()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    chunk, page, total_pages = _page_slice(all_items, page)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="thanks_add")],
        [InlineKeyboardButton(text="✍️ Ответ на благодарность", callback_data="thanks_reply")],
    ]
    for is_std, item in chunk:
        if is_std:
            rows.append(
                [InlineKeyboardButton(text=f"📌 {_short(item.get('phrase', ''), 28)}", callback_data="noop")]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 {_short(item.get('phrase', ''), 28)}",
                        callback_data=f"del_thanks_{item['id']}",
                    )
                ]
            )
    rows.extend(_nav_rows("thanks_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_words")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _end_msg_text(user_id: int, page: int = 0) -> str:
    user_items = settings_store.get_user_thanks(user_id)
    std_items = settings_store.get_standard_thanks()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    chunk, page, total_pages = _page_slice(all_items, page)
    reply = (settings_store.get_user_thanks_reply(user_id) or "").strip()
    if reply:
        reply_line = f"Ответ на благодарность: <i>«{html.escape(reply)}»</i>"
    else:
        reply_line = "Ответ на благодарность: <i>не задан — робот молчит"
    lines = [
        "🙏 <b>End msg</b>\n",
        "Клиент прислал голую благодарность → робот отправляет «ответ на благодарность» (без задачи).\n",
        "📌 — стандартные (нельзя удалить).\n",
        reply_line + "\n",
        f"<b>Слова — {page + 1}/{total_pages}:</b>",
    ]
    if chunk:
        for is_std, item in chunk:
            prefix = "📌" if is_std else "•"
            lines.append(f"{prefix} «{html.escape(item.get('phrase', ''))}»")
    else:
        lines.append("<i>пока пусто</i>")
    lines.append("\nНажмите 🗑 чтобы удалить слово, ➕ чтобы добавить.")
    return "\n".join(lines)


def lib_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    library = settings_store.get_user_library(user_id)
    chunk, page, total_pages = _page_slice(library, page)
    rows: list[list[InlineKeyboardButton]] = []
    for item in chunk:
        title = _short(item.get("title", ""), 28)
        rows.append(
            [
                InlineKeyboardButton(text=f"📄 {title}", callback_data=f"lib_show_{item['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"lib_del_{item['id']}"),
            ]
        )
    rows.extend(_nav_rows("lib_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="➕ Добавить текст", callback_data="lib_add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="menu_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _lib_text(user_id: int, page: int = 0) -> str:
    library = settings_store.get_user_library(user_id)
    if not library:
        return (
            "📚 <b>Справочник</b>\n\n"
            "Пока пуст. Сохраните сюда свои тексты — потом откроете и перешлёте клиенту."
        )
    chunk, page, total_pages = _page_slice(library, page)
    lines = [
        "📚 <b>Справочник</b>\n",
        f"<b>Тексты — {page + 1}/{total_pages}:</b>",
    ]
    for item in chunk:
        lines.append(f"<b>{item['id']}.</b> {_short(item.get('title', ''), 40)}")
    lines.append(
        "\nНажмите 📄 — бот пришлёт текст сообщением, перешлёте клиенту. 🗑 — удалить."
    )
    return "\n".join(lines)


def add_chats_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    af = _get_add_filter(user_id)
    dialog_cache = _get_dialog_cache(user_id)
    watch_ids = settings_store.get_user_allowed_chat_ids(user_id)
    available = [item for item in dialog_cache if item[0] not in watch_ids]
    if af == "groups":
        available = [item for item in available if item[2] == "group"]
    elif af == "contacts":
        available = [item for item in available if item[2] == "client"]
    total_pages = max(1, (len(available) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = available[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    g_mark = "🔘" if af == "groups" else "⚪️"
    c_mark = "🔘" if af == "contacts" else "⚪️"
    rows.append([
        InlineKeyboardButton(text=f"{g_mark} Группы", callback_data="add_filter_groups"),
        InlineKeyboardButton(text=f"{c_mark} Контакты", callback_data="add_filter_contacts"),
    ])
    for chat_id, title, chat_type in chunk:
        kind = _kind_label(chat_type)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ {_short(title, 28)} ({kind})",
                    callback_data=f"add_chat_{chat_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"add_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"add_page_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить список из Telegram", callback_data="menu_add_chat")]
    )
    rows.append([InlineKeyboardButton(text="✅ Выбрать несколько", callback_data="sel_start")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="menu_chats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _add_chats_text(user_id: int, page: int = 0) -> str:
    af = _get_add_filter(user_id)
    dialog_cache = _get_dialog_cache(user_id)
    watch_ids = settings_store.get_user_allowed_chat_ids(user_id)
    available = [item for item in dialog_cache if item[0] not in watch_ids]
    if af == "groups":
        available = [item for item in available if item[2] == "group"]
    elif af == "contacts":
        available = [item for item in available if item[2] == "client"]
    if not dialog_cache:
        return (
            "➕ <b>Добавить</b>\n\n"
            "Список диалогов пуст. Проверьте авторизацию личного аккаунта."
        )
    if not available:
        return (
            "➕ <b>Добавить</b>\n\n"
            "Все доступные уже в списке."
        )
    total_pages = max(1, (len(available) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    return (
        "➕ <b>Добавить</b>\n\n"
        "Нажмите на чат, чтобы включить там автоответ.\n"
        f"Страница {page + 1} из {total_pages}."
    )


def sel_chats_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    dialog_cache = _get_dialog_cache(user_id)
    sel = _get_selection(user_id)
    watch_ids = settings_store.get_user_allowed_chat_ids(user_id)
    available = [item for item in dialog_cache if item[0] not in watch_ids]
    total_pages = max(1, (len(available) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = available[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for chat_id, title, chat_type in chunk:
        mark = "☑" if chat_id in sel else "☐"
        kind = _kind_label(chat_type)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {_short(title, 28)} ({kind})",
                    callback_data=f"sel_toggle_{chat_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"sel_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"sel_page_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text=f"✅ Добавить выбранные ({len(sel)})",
                callback_data="sel_apply",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sel_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sel_text(user_id: int, page: int = 0) -> str:
    dialog_cache = _get_dialog_cache(user_id)
    sel = _get_selection(user_id)
    watch_ids = settings_store.get_user_allowed_chat_ids(user_id)
    available = [item for item in dialog_cache if item[0] not in watch_ids]
    if not dialog_cache:
        return (
            "✅ <b>Массовое добавление</b>\n\n"
            "Список диалогов пуст. Проверьте авторизацию личного аккаунта."
        )
    if not available:
        return (
            "✅ <b>Массовое добавление</b>\n\n"
            "Все доступные группы и клиенты уже в списке автоответов."
        )
    total_pages = max(1, (len(available) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    return (
        "✅ <b>Массовое добавление</b>\n\n"
        "Отмечайте ☑ нужные группы и клиенты, затем нажмите «Добавить выбранные».\n"
        f"Выбрано: {len(sel)}.\n"
        f"Страница {page + 1} из {total_pages}."
    )


def excluded_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    items = list(settings_store.get_user_excluded_users(user_id).items())
    chunk, page, total_pages = _page_slice(items, page)
    rows: list[list[InlineKeyboardButton]] = []
    for uid, info in chunk:
        name = info.get("name", "")
        username = info.get("username", "")
        label = _short(name or username or uid, 32)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"❌ {label}",
                    callback_data=f"del_excluded_{uid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="menu_add_excluded")]
    )
    rows.extend(_nav_rows("excluded_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _excluded_text(user_id: int, page: int = 0) -> str:
    users = settings_store.get_user_excluded_users(user_id)
    if not users:
        return (
            "🚫 <b>Исключения</b>\n\n"
            "Список пуст. Бот отвечает всем.\n"
            "Нажмите «Добавить», выберите контакт из Telegram."
        )
    items = list(users.items())
    chunk, page, total_pages = _page_slice(items, page)
    lines = [f"🚫 <b>Бот не отвечает этим людям — {page + 1}/{total_pages}:</b>\n"]
    for uid, info in chunk:
        name = info.get("name", "")
        username = info.get("username", "")
        label = html.escape(name or username or str(uid))
        lines.append(f"• {label}")
    lines.append("\nНажмите ❌ чтобы убрать из списка.")
    return "\n".join(lines)


def excluded_contacts_keyboard(owner_id: int, page: int = 0) -> InlineKeyboardMarkup:
    contact_cache = _get_contact_cache(owner_id)
    excluded_ids = set(int(uid) for uid in settings_store.get_user_excluded_users(owner_id).keys())
    available = [item for item in contact_cache if item[0] not in excluded_ids]
    total_pages = max(1, (len(available) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = available[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for user_id, name, _ in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ {_short(name, 32)}",
                    callback_data=f"excluded_contact_{user_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"excl_contact_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"excl_contact_page_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить контакты", callback_data="menu_add_excluded")]
    )
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_excluded")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def no_action_phrase_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    user_items = settings_store.get_user_no_action_phrases(user_id)
    std_items = settings_store.get_standard_no_action_phrases()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    chunk, page, total_pages = _page_slice(all_items, page)
    rows: list[list[InlineKeyboardButton]] = []
    for is_std, item in chunk:
        phrase = item.get("phrase", "")
        if is_std:
            rows.append(
                [InlineKeyboardButton(text=f"📌 {_short(phrase, 32)}", callback_data="noop")]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"❌ {_short(phrase, 32)}",
                        callback_data=f"del_no_action_phrase_{item.get('id')}",
                    )
                ]
            )
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить фразу", callback_data="menu_add_no_action_phrase")]
    )
    rows.extend(_nav_rows("no_action_phrase_page_", page, total_pages))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_words")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _no_action_phrase_text(user_id: int, page: int = 0) -> str:
    user_items = settings_store.get_user_no_action_phrases(user_id)
    std_items = settings_store.get_standard_no_action_phrases()
    all_items = [(True, item) for item in std_items] + [(False, item) for item in user_items]
    if not all_items:
        return (
            "⏸ <b>Без ответа — фразы</b>\n\n"
            "Список пуст. Бот реагирует на все сообщения.\n"
            "Нажмите «Добавить», чтобы добавить фразу."
        )
    chunk, page, total_pages = _page_slice(all_items, page)
    lines = [f"⏸ <b>Фразы без ответа — {page + 1}/{total_pages}:</b>\n"]
    lines.append("📌 — стандартные (нельзя удалить).\n")
    for is_std, item in chunk:
        prefix = "📌" if is_std else "•"
        phrase = html.escape(item.get("phrase", ""))
        lines.append(f"{prefix} {phrase}")
    lines.append("\nНажмите ❌ чтобы удалить фразу.")
    return "\n".join(lines)


async def _safe_edit(message: Message, text: str, reply_markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        msg = str(exc).lower()
        if "query is too old" not in msg and "query id is invalid" not in msg:
            logger.warning("Failed to answer callback query: %s", exc)
    except Exception as exc:
        logger.warning("Failed to answer callback query: %s", exc)


async def _load_dialogs(client: TelegramClient) -> list[tuple[int, str, str]]:
    dialogs: list[tuple[int, str, str]] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User) and not getattr(entity, "bot", False):
            chat_type = "client"
        elif isinstance(entity, Chat) or (
            isinstance(entity, Channel) and getattr(entity, "megagroup", False)
        ):
            chat_type = "group"
        else:
            continue
        dialogs.append((dialog.id, dialog.name or str(dialog.id), chat_type))
    return dialogs


def _format_code_prompt(
    buffer: str,
    code_length: int = 5,
    is_checking: bool = False,
    title: str = "Код подтверждения",
) -> str:
    """Форматирует красивое отображение вводимого кода со слотами для каждой цифры."""
    entered = list(buffer[:code_length])
    remaining = max(0, code_length - len(entered))
    slots = [f"<b>{d}</b>" for d in entered] + ["<b>•</b>" for _ in range(remaining)]
    code_display = "  ".join(slots) if slots else "<b>•  •  •  •  •</b>"

    if is_checking or remaining == 0:
        return (
            f"🔐 <b>{title}:</b>\n\n"
            f"👉  {code_display}\n\n"
            f"⏳ <i>Проверяю код... Пожалуйста, подождите.</i>"
        )

    if remaining % 10 == 1 and remaining % 100 != 11:
        rem_text = f"осталась {remaining} цифра"
    elif 2 <= remaining % 10 <= 4 and not (12 <= remaining % 100 <= 14):
        rem_text = f"осталось {remaining} цифры"
    else:
        rem_text = f"осталось {remaining} цифр"

    return (
        f"🔐 <b>{title}</b> (вводите кнопками или сообщением):\n\n"
        f"👉  {code_display}\n\n"
        f"<i>(Введено: {len(entered)} из {code_length}, {rem_text})</i>"
    )


def _code_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=str(d), callback_data=f"auth_digit:{d}") for d in (1, 2, 3)],
        [InlineKeyboardButton(text=str(d), callback_data=f"auth_digit:{d}") for d in (4, 5, 6)],
        [InlineKeyboardButton(text=str(d), callback_data=f"auth_digit:{d}") for d in (7, 8, 9)],
        [
            InlineKeyboardButton(text="⌫ Стереть", callback_data="auth_del"),
            InlineKeyboardButton(text="0", callback_data="auth_digit:0"),
            InlineKeyboardButton(text="✅ Ввести", callback_data="auth_ok"),
        ],
        [InlineKeyboardButton(text="🔄 Запросить код повторно", callback_data="auth_resend")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="auth_cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _submit_code(message: Message, code: str, state: FSMContext) -> None:
    data = await state.get_data()
    phone = data.get("phone", "")
    phone_code_hash = data.get("phone_code_hash", "")
    client: TelegramClient = router.telethon_client_getter()
    if client is None:
        err_msg = "❌ Telethon-клиент не запущен."
        if message.from_user.is_bot:
            await _safe_edit(message, err_msg, get_back_keyboard("menu_auth"))
        else:
            await message.answer(err_msg, reply_markup=get_back_keyboard("menu_auth"))
        return

    try:
        if not client.is_connected():
            await client.connect()
        if phone_code_hash:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        else:
            await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        await state.set_state(AuthStates.waiting_for_password)
        prompt_text = (
            "🔒 На аккаунте включён облачный пароль (2FA).\n"
            "Отправьте пароль следующим сообщением.\n\n"
            "Сообщение с паролем бот удалит сразу после получения."
        )
        if message.from_user.is_bot:
            await _safe_edit(message, prompt_text, get_back_keyboard("menu_auth"))
        else:
            await message.answer(prompt_text, reply_markup=get_back_keyboard("menu_auth"))
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        await state.update_data(code_buffer="")
        err_detail = "истёк" if isinstance(exc, PhoneCodeExpiredError) else "неверный"
        code_length = data.get("code_length", 5)
        prompt_text = (
            f"❌ <b>Код {err_detail}.</b>\n\n"
            f"{_format_code_prompt('', code_length, title='Код из Telegram')}\n\n"
            "💡 Введите код заново кнопками или сообщением, либо нажмите «🔄 Запросить код повторно»:"
        )
        if message.from_user.is_bot:
            await _safe_edit(message, prompt_text, _code_keyboard())
        else:
            prompt_msg_id = data.get("prompt_msg_id")
            if prompt_msg_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_msg_id,
                        text=prompt_text,
                        reply_markup=_code_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    pass
            await message.answer(prompt_text, reply_markup=_code_keyboard(), parse_mode="HTML")
        return
    except Exception as exc:
        logger.exception("Failed to sign in with code")
        await state.update_data(code_buffer="")
        code_length = data.get("code_length", 5)
        prompt_text = (
            f"❌ <b>Ошибка входа:</b> {html.escape(str(exc))}\n\n"
            f"{_format_code_prompt('', code_length, title='Код из Telegram')}\n\n"
            "💡 Введите код заново кнопками или сообщением:"
        )
        if message.from_user.is_bot:
            await _safe_edit(message, prompt_text, _code_keyboard())
        else:
            prompt_msg_id = data.get("prompt_msg_id")
            if prompt_msg_id:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_msg_id,
                        text=prompt_text,
                        reply_markup=_code_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    pass
            await message.answer(prompt_text, reply_markup=_code_keyboard(), parse_mode="HTML")
        return

    uid = message.from_user.id
    creds = settings_store.get_user_credentials(uid)
    if creds:
        settings_store.save_user_credentials(
            user_id=uid,
            phone=creds.get("phone", phone),
            api_id=creds.get("api_id", 0),
            api_hash=creds.get("api_hash", ""),
            activated=True,
        )

    await state.clear()
    success_text = (
        "🎉 <b>Авторизация успешна!</b> Сессия сохранена — "
        "при следующих запусках вход не потребуется."
    )
    if message.from_user.is_bot:
        await _safe_edit(message, success_text, get_main_menu_keyboard(uid))
    else:
        await message.answer(success_text, reply_markup=get_main_menu_keyboard(uid), parse_mode="HTML")


def register_admin_handlers(dp: Dispatcher, telethon_client_getter) -> None:
    router.telethon_client_getter = telethon_client_getter

    @dp.error()
    async def global_error_handler(event, exception):
        if isinstance(exception, TelegramBadRequest):
            msg = str(exception).lower()
            if "query is too old" in msg or "query id is invalid" in msg or "message is not modified" in msg:
                return True
        logger.error("Error handling update: %s", exception, exc_info=exception)
        return True

    @router.message(Command("start"))
    async def cmd_start(message: Message):
        uid = message.from_user.id
        username = message.from_user.username or ""
        first = message.from_user.first_name or ""
        last = message.from_user.last_name or ""
        name = f"{first} {last}".strip() or username or f"User {uid}"
        settings_store.add_subscriber(
            telegram_id=uid,
            name=name,
            username=username,
            source="bot_start",
        )
        old_menu_id = settings_store.get_last_menu_message_id(uid)
        logger.info(f"Old menu ID for user {uid}: {old_menu_id}")
        if old_menu_id is not None:
            try:
                from aiogram.exceptions import TelegramBadRequest
                await message.bot.delete_message(chat_id=message.chat.id, message_id=old_menu_id)
                logger.info(f"Deleted old menu {old_menu_id} for user {uid}")
            except TelegramBadRequest as e:
                logger.warning(f"Cannot delete message {old_menu_id}: {e}")
            except Exception as e:
                logger.warning(f"Failed to delete old menu {old_menu_id}: {e}")
        try:
            await message.delete()
        except Exception:
            pass
        new_msg = await message.answer(
            _home_text(uid),
            reply_markup=get_main_menu_keyboard(uid),
            parse_mode="HTML",
        )
        settings_store.set_last_menu_message_id(uid, new_msg.message_id)
        logger.info(f"Saved new menu ID {new_msg.message_id} for user {uid}")

    @router.callback_query(F.data == "menu_home")
    async def cb_home(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _home_text(uid), get_main_menu_keyboard(uid))
        await callback.answer()

    @router.callback_query(F.data == "menu_assistant")
    async def cb_assistant(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        if uid != config.admin_user_id and not _user_is_activated(uid):
            client = _pending_auth_clients.pop(uid, None)
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            _clean_user_session(uid)
            await state.set_state(SettingsStates.waiting_for_user_creds)
            await state.update_data(setup_step="phone")
            phone_kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await _safe_edit(
                callback.message,
                "📱 <b>Настройка помощника</b>\n\n"
                "Нажмите кнопку ниже, чтобы отправить номер вашего Telegram-аккаунта (или введите номер в формате +998...):",
            )
            await callback.message.answer("Нажмите кнопку или отправьте номер сообщением:", reply_markup=phone_kb)
            await callback.answer()
            return
        await _safe_edit(callback.message, _assistant_text(uid), _assistant_keyboard_for_user(uid))
        await callback.answer()

    @router.callback_query(F.data == "menu_user_settings")
    async def _render_user_settings(callback: CallbackQuery):
        uid = callback.from_user.id
        creds = settings_store.get_user_credentials(uid)
        phone = creds.get("phone", "не указан") if creds else "не указан"
        status = "Активен" if _user_is_activated(uid) or uid == config.admin_user_id else "Не активирован"
        chats = settings_store.get_user_watchlist(uid)
        groups_count = sum(1 for c in chats.values() if c.get("type") == "group")
        clients_count = sum(1 for c in chats.values() if c.get("type") == "client")
        tpl_count = len(settings_store.get_user_templates(uid))
        task_count = len(settings_store.get_user_tasks(uid, done=False))
        lines = [
            "⚙️ <b>Настройки</b>\n",
            f"<b>📕 ID:</b> <code>{uid}</code>",
            f"<b>💻 Телефон:</b> {html.escape(phone)}\n",
            f"<b>🎯 Статус:</b> {status}",
            f"<b>👥 Группы:</b> {groups_count}",
            f"<b>👤 Клиенты:</b> {clients_count}",
            f"<b>📝 Шаблонов:</b> {tpl_count}",
            f"<b>Открытых задач:</b> {task_count}",
        ]
        text = "\n".join(lines)
        rows: list[list[InlineKeyboardButton]] = []
        if uid != config.admin_user_id:
            rows.append([InlineKeyboardButton(text="🔐 Выход", callback_data="menu_logout")])
            rows.append([InlineKeyboardButton(text="🧹 Очистить кеш", callback_data="menu_clear_cache")])
            rows.append([InlineKeyboardButton(text="🔺 Сбросить все данные", callback_data="menu_reset_all")])
            rows.append([InlineKeyboardButton(text="❌ Удалить аккаунт", callback_data="menu_delete_account")])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")])
        await _safe_edit(
            callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows)
        )

    @router.callback_query(F.data == "menu_user_settings")
    async def cb_user_settings(callback: CallbackQuery):
        await _render_user_settings(callback)
        await callback.answer()

    @router.callback_query(F.data == "menu_reset_all")
    async def cb_reset_all(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid == config.admin_user_id:
            await callback.answer("Админ не может сбросить данные", show_alert=True)
            return
        settings_store.clear_user_data(uid)
        await callback.answer("Все данные сброшены")
        await _safe_edit(
            callback.message,
            "✅ Все ваши данные сброшены. Помощник сброшен.",
            _assistant_keyboard_for_user(uid),
        )

    @router.callback_query(F.data == "menu_clear_cache")
    async def cb_clear_cache(callback: CallbackQuery):
        uid = callback.from_user.id
        _dialog_cache.pop(uid, None)
        _contact_cache.pop(uid, None)
        _get_selection(uid).clear()
        _chats_filter.pop(uid, None)
        _add_filter.pop(uid, None)
        await _render_user_settings(callback)
        await callback.answer("Кеш очищен")

    @router.callback_query(F.data == "menu_delete_account")
    async def cb_delete_account_confirm(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid == config.admin_user_id:
            await callback.answer("Админ не может удалить аккаунт", show_alert=True)
            return
        await _safe_edit(
            callback.message,
            "️ <b>Удаление аккаунта</b>\n\n"
            "Это действие <b>необратимо</b>. Будут удалены:\n"
            "• Все настройки помощника\n"
            "• Все шаблоны и сообщения\n"
            "• Все задачи\n"
            "• Все группы и клиенты\n"
            "• Данные авторизации Telegram\n\n"
            "Нажмите «✅ Да, удалить» для подтверждения или «Отмена».",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, удалить", callback_data="delete_account_yes")],
                    [InlineKeyboardButton(text="Отмена", callback_data="menu_user_settings")],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "delete_account_yes")
    async def cb_delete_account_yes(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid == config.admin_user_id:
            await callback.answer("Админ не может удалить аккаунт", show_alert=True)
            return
        settings_store.delete_user_account(uid)
        await callback.answer("Аккаунт удалён")
        await _safe_edit(
            callback.message,
            "✅ Ваш аккаунт полностью удалён.\n\n"
            "Для повторного использования нажмите «🤖 Помощник» и пройдите настройку.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu_home")],
                ]
            ),
        )

    @router.callback_query(F.data == "menu_admin_panel")
    async def cb_admin_panel(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        all_creds = settings_store.get_all_credentials()
        activated = [c for c in all_creds if c.get("activated")]
        user_ids = settings_store.get_all_user_ids()
        std_greet_count = len(settings_store.get_standard_greetings())
        std_thanks_count = len(settings_store.get_standard_thanks())
        std_no_action_count = len(settings_store.get_standard_no_action_phrases())
        lines = [
            "🛡 <b>Админ-панель</b>\n",
            f"<b>Пользователей всего:</b> {len(user_ids)}",
            f"<b>Подключено (активны):</b> {len(activated)}",
            f"<b>Стандартных Start msg:</b> {std_greet_count}",
            f"<b>Стандартных End msg:</b> {std_thanks_count}",
            f"<b>Стандартных Без ответа:</b> {std_no_action_count}",
        ]
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Стандарты", callback_data="menu_admin_standards")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")],
            ]
        )
        await _safe_edit(callback.message, text, kb)
        await callback.answer()

    @router.callback_query(F.data == "menu_admin_standards")
    async def cb_admin_standards(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        std_greet_count = len(settings_store.get_standard_greetings())
        std_thanks_count = len(settings_store.get_standard_thanks())
        std_no_action_count = len(settings_store.get_standard_no_action_phrases())
        total = std_greet_count + std_thanks_count + std_no_action_count
        text = (
            "📋 <b>Стандарты</b>\n\n"
            "Эти слова добавляются всем пользователям автоматически.\n"
            f"Всего стандартных записей: {total}\n\n"
            "Выберите раздел:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🟢 Start msg ({std_greet_count})", callback_data="menu_std_start")],
                [InlineKeyboardButton(text=f"🙏 End msg ({std_thanks_count})", callback_data="menu_std_end")],
                [InlineKeyboardButton(text=f"⏸ Без ответа ({std_no_action_count})", callback_data="menu_std_no_action")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_admin_panel")],
            ]
        )
        await _safe_edit(callback.message, text, kb)
        await callback.answer()

    @router.callback_query(F.data == "menu_std_start")
    async def cb_std_start(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        await _safe_edit(callback.message, _std_start_msg_text(0), _std_start_msg_keyboard(0))
        await callback.answer()

    @router.callback_query(F.data == "menu_std_end")
    async def cb_std_end(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        await _safe_edit(callback.message, _std_end_msg_text(0), _std_end_msg_keyboard(0))
        await callback.answer()

    @router.callback_query(F.data == "menu_std_no_action")
    async def cb_std_no_action(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.clear()
        await _safe_edit(callback.message, _std_no_action_text(0), _std_no_action_keyboard(0))
        await callback.answer()

    def _std_start_msg_keyboard(page: int = 0) -> InlineKeyboardMarkup:
        items = settings_store.get_standard_greetings()
        chunk, page, total_pages = _page_slice(items, page)
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="➕ Добавить", callback_data="std_greet_add")]
        ]
        for item in chunk:
            label = f"{_short(item.get('phrase', ''), 18)} → {_short(item.get('reply', ''), 22)}"
            rows.append(
                [InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"del_std_greet_{item['id']}")]
            )
        rows.extend(_nav_rows("std_greet_page_", page, total_pages))
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_admin_standards")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _std_start_msg_text(page: int = 0) -> str:
        items = settings_store.get_standard_greetings()
        if not items:
            return (
                "🟢 <b>Стандартные Start msg</b>\n\n"
                "Список пуст. Добавьте стандартные приветствия."
            )
        chunk, page, total_pages = _page_slice(items, page)
        lines = [
            "🟢 <b>Стандартные Start msg</b>\n",
            f"<b>Слова — {page + 1}/{total_pages}:</b>",
        ]
        for item in chunk:
            lines.append(
                f"• «{html.escape(item.get('phrase', ''))}» → "
                f"<i>«{html.escape(item.get('reply', ''))}»</i>"
            )
        lines.append("\nНажмите 🗑 чтобы удалить, ➕ чтобы добавить.")
        return "\n".join(lines)

    def _std_end_msg_keyboard(page: int = 0) -> InlineKeyboardMarkup:
        items = settings_store.get_standard_thanks()
        chunk, page, total_pages = _page_slice(items, page)
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="➕ Добавить", callback_data="std_thanks_add")]
        ]
        for item in chunk:
            rows.append(
                [InlineKeyboardButton(text=f"🗑 {_short(item.get('phrase', ''), 28)}", callback_data=f"del_std_thanks_{item['id']}")]
            )
        rows.extend(_nav_rows("std_thanks_page_", page, total_pages))
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_admin_standards")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _std_end_msg_text(page: int = 0) -> str:
        items = settings_store.get_standard_thanks()
        if not items:
            return (
                "🙏 <b>Стандартные End msg</b>\n\n"
                "Список пуст. Добавьте стандартные благодарности."
            )
        chunk, page, total_pages = _page_slice(items, page)
        lines = [
            "🙏 <b>Стандартные End msg</b>\n",
            f"<b>Слова — {page + 1}/{total_pages}:</b>",
        ]
        for item in chunk:
            lines.append(f"• «{html.escape(item.get('phrase', ''))}»")
        lines.append("\nНажмите 🗑 чтобы удалить, ➕ чтобы добавить.")
        return "\n".join(lines)

    def _std_no_action_keyboard(page: int = 0) -> InlineKeyboardMarkup:
        items = settings_store.get_standard_no_action_phrases()
        chunk, page, total_pages = _page_slice(items, page)
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="➕ Добавить", callback_data="std_no_action_add")]
        ]
        for item in chunk:
            rows.append(
                [InlineKeyboardButton(text=f"❌ {_short(item.get('phrase', ''), 32)}", callback_data=f"del_std_no_action_{item['id']}")]
            )
        rows.extend(_nav_rows("std_no_action_page_", page, total_pages))
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_admin_standards")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _std_no_action_text(page: int = 0) -> str:
        items = settings_store.get_standard_no_action_phrases()
        if not items:
            return (
                "⏸ <b>Стандартные «Без ответа»</b>\n\n"
                "Список пуст. Добавьте стандартные фразы."
            )
        chunk, page, total_pages = _page_slice(items, page)
        lines = [
            "⏸ <b>Стандартные фразы без ответа</b>\n",
            f"<b>Фразы — {page + 1}/{total_pages}:</b>",
        ]
        for item in chunk:
            lines.append(f"• «{html.escape(item.get('phrase', ''))}»")
        lines.append("\nНажмите ❌ чтобы удалить, ➕ чтобы добавить.")
        return "\n".join(lines)

    @router.callback_query(F.data.startswith("std_greet_page_"))
    async def cb_std_greet_page(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            page = int(callback.data.replace("std_greet_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _std_start_msg_text(page), _std_start_msg_keyboard(page))
        await callback.answer()

    @router.callback_query(F.data == "std_greet_add")
    async def cb_std_greet_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.set_state(SettingsStates.waiting_for_std_greet_phrase)
        await _safe_edit(
            callback.message,
            "🟢 <b>Новое стандартное Start msg</b>\n\n"
            "Отправьте <b>фразу-приветствие</b>:\n"
            "Например: <i>Добрый день</i>",
            get_back_keyboard("menu_std_start"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_std_greet_phrase)
    async def process_std_greet_phrase(message: Message, state: FSMContext):
        if message.from_user.id != config.admin_user_id:
            return
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Текст не может быть пустым. Отправьте фразу:")
            return
        await state.update_data(phrase=phrase)
        await state.set_state(SettingsStates.waiting_for_std_greet_reply)
        await message.answer(
            "Теперь отправьте <b>ответ</b> на это приветствие:",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("menu_std_start"),
        )

    @router.message(SettingsStates.waiting_for_std_greet_reply)
    async def process_std_greet_reply(message: Message, state: FSMContext):
        if message.from_user.id != config.admin_user_id:
            return
        reply = (message.text or "").strip()
        if not reply:
            await message.answer("Текст не может быть пустым. Отправьте ответ:")
            return
        data = await state.get_data()
        phrase = data.get("phrase", "")
        settings_store.add_standard_greeting(phrase, reply)
        await state.clear()
        await message.answer(
            "✅ Стандартное Start msg добавлено.\n\n"
            f"«{html.escape(phrase)}» → <i>«{html.escape(reply)}»</i>",
            parse_mode="HTML",
            reply_markup=_std_start_msg_keyboard(0),
        )

    @router.callback_query(F.data.startswith("del_std_greet_"))
    async def cb_del_std_greet(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            greeting_id = int(callback.data.replace("del_std_greet_", "", 1))
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        removed = settings_store.remove_standard_greeting(greeting_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        await _safe_edit(callback.message, _std_start_msg_text(0), _std_start_msg_keyboard(0))

    @router.callback_query(F.data.startswith("std_thanks_page_"))
    async def cb_std_thanks_page(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            page = int(callback.data.replace("std_thanks_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _std_end_msg_text(page), _std_end_msg_keyboard(page))
        await callback.answer()

    @router.callback_query(F.data == "std_thanks_add")
    async def cb_std_thanks_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.set_state(SettingsStates.waiting_for_std_thanks)
        await _safe_edit(
            callback.message,
            "🙏 <b>Новое стандартное End msg</b>\n\n"
            "Отправьте <b>слово благодарности</b>:\n"
            "Например: <i>Рахмат</i>",
            get_back_keyboard("menu_std_end"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_std_thanks)
    async def process_std_thanks(message: Message, state: FSMContext):
        if message.from_user.id != config.admin_user_id:
            return
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Текст не может быть пустым. Отправьте слово:")
            return
        settings_store.add_standard_thanks(phrase)
        await state.clear()
        await message.answer(
            f"✅ Стандартное End msg добавлено: <i>«{html.escape(phrase)}»</i>",
            parse_mode="HTML",
            reply_markup=_std_end_msg_keyboard(0),
        )

    @router.callback_query(F.data.startswith("del_std_thanks_"))
    async def cb_del_std_thanks(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            thanks_id = int(callback.data.replace("del_std_thanks_", "", 1))
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        removed = settings_store.remove_standard_thanks(thanks_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        await _safe_edit(callback.message, _std_end_msg_text(0), _std_end_msg_keyboard(0))

    @router.callback_query(F.data.startswith("std_no_action_page_"))
    async def cb_std_no_action_page(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            page = int(callback.data.replace("std_no_action_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _std_no_action_text(page), _std_no_action_keyboard(page))
        await callback.answer()

    @router.callback_query(F.data == "std_no_action_add")
    async def cb_std_no_action_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        await state.set_state(SettingsStates.waiting_for_std_no_action)
        await _safe_edit(
            callback.message,
            "⏸ <b>Новая стандартная фраза «Без ответа»</b>\n\n"
            "Отправьте фразу, на которую бот не будет реагировать:\n"
            "Например: <i>понятно</i>, <i>ясно</i>, <i>ок</i>",
            get_back_keyboard("menu_std_no_action"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_std_no_action)
    async def process_std_no_action(message: Message, state: FSMContext):
        if message.from_user.id != config.admin_user_id:
            return
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Фраза не может быть пустой. Отправьте фразу:")
            return
        settings_store.add_standard_no_action_phrase(phrase)
        await state.clear()
        await message.answer(
            f"✅ Стандартная фраза добавлена: <i>«{html.escape(phrase)}»</i>",
            parse_mode="HTML",
            reply_markup=_std_no_action_keyboard(0),
        )

    @router.callback_query(F.data.startswith("del_std_no_action_"))
    async def cb_del_std_no_action(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid != config.admin_user_id:
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            phrase_id = int(callback.data.replace("del_std_no_action_", "", 1))
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        removed = settings_store.remove_standard_no_action_phrase(phrase_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        await _safe_edit(callback.message, _std_no_action_text(0), _std_no_action_keyboard(0))

    @router.callback_query(F.data == "noop")
    async def cb_noop(callback: CallbackQuery):
        await callback.answer()

    # --- Пер-user handlers ---

    @router.callback_query(F.data == "menu_chats")
    async def cb_chats(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        _chats_filter[uid] = "all"
        await state.clear()
        await _safe_edit(callback.message, _chats_text(uid, 0), chats_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data.startswith("chat_page_"))
    async def cb_chat_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("chat_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _chats_text(uid, page), chats_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data.startswith("del_chat_"))
    async def cb_del_chat(callback: CallbackQuery):
        uid = callback.from_user.id
        cid = callback.data.replace("del_chat_", "", 1)
        try:
            settings_store.remove_user_chat(uid, int(cid))
            rebuild_chat_owner_map()
        except ValueError:
            await callback.answer("Некорректный чат", show_alert=True)
            return
        await callback.answer("Убрано из автоответов")
        cf = _get_chats_filter(uid)
        items = list(settings_store.get_user_watchlist(uid).items())
        if cf == "groups":
            items = [(c, i) for c, i in items if i.get("type") == "group"]
        elif cf == "contacts":
            items = [(c, i) for c, i in items if i.get("type") == "client"]
        _, page, _ = _page_slice(items, 0)
        await _safe_edit(callback.message, _chats_text(uid, page), chats_keyboard(uid, page))

    @router.callback_query(F.data == "menu_add_chat")
    async def cb_add_chat_menu(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer("Загружаю диалоги…")
        _add_filter[uid] = "all"
        try:
            client = await _get_client_for_user(uid)
            if not client or not client.is_connected():
                await _safe_edit(
                    callback.message,
                    "❌ Личный аккаунт Telegram не подключен или не запущен.",
                    get_back_keyboard("menu_chats"),
                )
                return
            _dialog_cache[uid] = await _load_dialogs(client)
            await _safe_edit(callback.message, _add_chats_text(uid, 0), add_chats_keyboard(uid, 0))
        except Exception as exc:
            logger.exception("Failed to load dialogs")
            await _safe_edit(
                callback.message,
                f"❌ Ошибка при получении диалогов: {exc}",
                get_back_keyboard("menu_chats"),
            )

    @router.callback_query(F.data.startswith("add_page_"))
    async def cb_add_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("add_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _add_chats_text(uid, page), add_chats_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data.startswith("add_chat_"))
    async def cb_add_chat(callback: CallbackQuery):
        uid = callback.from_user.id
        raw_id = callback.data.replace("add_chat_", "", 1)
        try:
            chat_id = int(raw_id)
        except ValueError:
            await callback.answer("Некорректный чат", show_alert=True)
            return
        dialog_cache = _get_dialog_cache(uid)
        match = next((item for item in dialog_cache if item[0] == chat_id), None)
        if match is None:
            await callback.answer("Чат не найден в списке, обновите диалоги", show_alert=True)
            return
        settings_store.add_user_chat(uid, match[0], match[1], match[2])
        rebuild_chat_owner_map()
        items = list(settings_store.get_user_watchlist(uid).items())
        _, page, _ = _page_slice(items, 10**9)
        await _safe_edit(callback.message, _chats_text(uid, page), chats_keyboard(uid, page))
        await callback.answer("Автоответ включён")

    @router.callback_query(F.data == "sel_start")
    async def cb_sel_start(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            client = await _get_client_for_user(uid)
            if not client or not client.is_connected():
                await _safe_edit(
                    callback.message,
                    "❌ Личный аккаунт Telegram не подключен или не запущен.",
                    get_back_keyboard("menu_chats"),
                )
                return
            await callback.answer("Загружаю диалоги…")
            _dialog_cache[uid] = await _load_dialogs(client)
        except Exception as exc:
            logger.exception("Failed to load dialogs")
            await _safe_edit(
                callback.message,
                f"❌ Ошибка при получении диалогов: {exc}",
                get_back_keyboard("menu_chats"),
            )
            return
        _get_selection(uid).clear()
        await _safe_edit(callback.message, _sel_text(uid, 0), sel_chats_keyboard(uid, 0))

    @router.callback_query(F.data.startswith("sel_page_"))
    async def cb_sel_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("sel_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _sel_text(uid, page), sel_chats_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data.startswith("sel_toggle_"))
    async def cb_sel_toggle(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            chat_id = int(callback.data.replace("sel_toggle_", "", 1))
        except ValueError:
            await callback.answer("Некорректный чат", show_alert=True)
            return
        sel = _get_selection(uid)
        if chat_id in sel:
            sel.discard(chat_id)
        else:
            sel.add(chat_id)
        dialog_cache = _get_dialog_cache(uid)
        watch_ids = settings_store.get_user_allowed_chat_ids(uid)
        available = [item[0] for item in dialog_cache if item[0] not in watch_ids]
        try:
            page = available.index(chat_id) // PAGE_SIZE
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _sel_text(uid, page), sel_chats_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data == "sel_apply")
    async def cb_sel_apply(callback: CallbackQuery):
        uid = callback.from_user.id
        sel = _get_selection(uid)
        if not sel:
            await callback.answer("Ничего не выбрано", show_alert=True)
            return
        dialog_cache = _get_dialog_cache(uid)
        by_id = {item[0]: item for item in dialog_cache}
        added = 0
        for chat_id in list(sel):
            item = by_id.get(chat_id)
            if item is None:
                continue
            settings_store.add_user_chat(uid, item[0], item[1], item[2])
            added += 1
        sel.clear()
        rebuild_chat_owner_map()
        await callback.answer(f"Добавлено: {added}")
        items = list(settings_store.get_user_watchlist(uid).items())
        _, page, _ = _page_slice(items, 10**9)
        await _safe_edit(callback.message, _chats_text(uid, page), chats_keyboard(uid, page))

    @router.callback_query(F.data == "sel_cancel")
    async def cb_sel_cancel(callback: CallbackQuery):
        uid = callback.from_user.id
        _get_selection(uid).clear()
        await callback.answer("Отменено")
        await _safe_edit(callback.message, _add_chats_text(uid, 0), add_chats_keyboard(uid, 0))

    @router.callback_query(F.data == "menu_tasks")
    async def cb_tasks(callback: CallbackQuery):
        uid = callback.from_user.id
        tasks = settings_store.get_user_tasks(uid, done=False)
        if tasks:
            header = (
                f"📌 <b>Открытые задачи — всего {len(tasks)}</b>\n\n"
                "Сейчас пришлю каждую отдельным сообщением."
            )
        else:
            header = "📌 <b>Задачи</b>\n\nОткрытых задач нет."
        await _safe_edit(callback.message, header, tasks_header_keyboard(uid))
        await callback.answer()
        for task in tasks:
            try:
                await callback.message.answer(
                    format_task_html(task),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="✅ Сделал",
                                    callback_data=f"task_done_{task['id']}",
                                )
                            ]
                        ]
                    ),
                )
            except Exception as exc:
                logger.error("Failed to send task post: %s", exc)
                break
            await asyncio.sleep(0.2)

    @router.callback_query(F.data.startswith("task_done_"))
    async def cb_task_done(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            task_id = int(callback.data.replace("task_done_", "", 1))
        except ValueError:
            await callback.answer("Некорректная задача", show_alert=True)
            return
        task = settings_store.complete_user_task(uid, task_id)
        if task is None:
            await callback.answer("Задача уже закрыта")
        else:
            await callback.answer("Задача закрыта — в разделе «Сделанные»")
        closed = (
            f"✅ <b>Задача {task_id} закрыта</b>"
            if task is None
            else f"✅ <b>Задача {task_id} закрыта</b>\n\n{format_task_html(task)}"
        )
        try:
            await callback.message.edit_text(closed, parse_mode="HTML")
        except TelegramBadRequest:
            pass

    @router.callback_query(F.data == "menu_tasks_done")
    async def cb_tasks_done(callback: CallbackQuery):
        uid = callback.from_user.id
        await _safe_edit(callback.message, _done_text(uid), done_menu_keyboard())
        await callback.answer()
        for task in settings_store.get_user_tasks(uid, done=True):
            try:
                await callback.message.answer(
                    format_task_html(task),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=f"🗑 Удалить · задача {task['id']}",
                                    callback_data=f"task_del_{task['id']}",
                                )
                            ]
                        ]
                    ),
                )
            except Exception as exc:
                logger.error("Failed to send done task post: %s", exc)
                break
            await asyncio.sleep(0.2)

    @router.callback_query(F.data == "tasks_clear_done")
    async def cb_tasks_clear_done(callback: CallbackQuery):
        uid = callback.from_user.id
        removed = settings_store.clear_user_done_tasks(uid)
        await callback.answer(f"Удалено задач: {removed}" if removed else "Список уже пуст")
        await _safe_edit(callback.message, _done_text(uid), done_menu_keyboard())

    @router.callback_query(F.data == "tasks_clear_all")
    async def cb_tasks_clear_all(callback: CallbackQuery):
        uid = callback.from_user.id
        removed = settings_store.clear_all_user_tasks(uid)
        await callback.answer(f"Удалено задач: {removed}" if removed else "Список уже пуст")
        await _safe_edit(callback.message, _done_text(uid), tasks_header_keyboard(uid))

    @router.callback_query(F.data.startswith("task_del_"))
    async def cb_task_del(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            task_id = int(callback.data.replace("task_del_", "", 1))
        except ValueError:
            await callback.answer("Некорректная задача", show_alert=True)
            return
        removed = settings_store.remove_user_task(uid, task_id)
        await callback.answer("Удалено" if removed else "Задача не найдена")
        try:
            await callback.message.delete()
        except Exception:
            try:
                await callback.message.edit_text("🗑 Задача удалена.")
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data == "menu_messages")
    async def cb_messages(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _messages_text(uid, 0), messages_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data.startswith("tpl_page_"))
    async def cb_tpl_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("tpl_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _messages_text(uid, page), messages_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data == "tpl_add")
    async def cb_tpl_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_question)
        await _safe_edit(
            callback.message,
            "💬 <b>Новый шаблон</b>\n\n"
            "Отправьте <b>вариант вопроса</b> клиента — фразу, по которой робот узнает запрос.\n"
            "Например: <i>помощь по рассылке</i>",
            get_back_keyboard("menu_messages"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_question)
    async def process_question(message: Message, state: FSMContext):
        uid = message.from_user.id
        question = (message.text or "").strip()
        if not question:
            await message.answer("Текст не может быть пустым. Отправьте вариант вопроса:")
            return
        await state.update_data(question=question)
        await state.set_state(SettingsStates.waiting_for_answer)
        await message.answer(
            "Теперь отправьте <b>текст ответа</b>, который отправит ваш аккаунт.\n"
            "Например: <i>Принято, сейчас дадим инструкцию</i>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("menu_messages"),
        )

    @router.message(SettingsStates.waiting_for_answer)
    async def process_answer(message: Message, state: FSMContext):
        uid = message.from_user.id
        answer = (message.text or "").strip()
        if not answer:
            await message.answer("Текст не может быть пустым. Отправьте ответ:")
            return
        data = await state.get_data()
        question = data.get("question", "")
        settings_store.add_user_template(uid, question, answer)
        await state.clear()
        await message.answer(
            "✅ Шаблон сохранён.\n\n"
            f"Вопрос: <i>«{html.escape(question)}»</i>\n"
            f"Ответ: <i>«{html.escape(answer)}»</i>",
            parse_mode="HTML",
            reply_markup=messages_keyboard(uid, 0),
        )

    @router.callback_query(F.data == "tpl_clear_all")
    async def cb_tpl_clear_all(callback: CallbackQuery):
        uid = callback.from_user.id
        removed = settings_store.clear_user_templates(uid)
        await callback.answer(f"Удалено шаблонов: {removed}" if removed else "Список уже пуст")
        await _safe_edit(callback.message, _messages_text(uid, 0), messages_keyboard(uid, 0))

    @router.callback_query(F.data.startswith("del_tpl_"))
    async def cb_del_tpl(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            template_id = int(callback.data.replace("del_tpl_", "", 1))
        except ValueError:
            await callback.answer("Некорректный шаблон", show_alert=True)
            return
        if settings_store.remove_user_template(uid, template_id):
            await callback.answer("Шаблон удалён")
        else:
            await callback.answer("Шаблон не найден")
        items = settings_store.get_user_templates(uid)
        _, page, _ = _page_slice(items, 0)
        await _safe_edit(callback.message, _messages_text(uid, page), messages_keyboard(uid, page))

    @router.callback_query(F.data == "toggle_assistant")
    async def cb_toggle_assistant(callback: CallbackQuery):
        uid = callback.from_user.id
        if not _assistant_on():
            await _safe_answer(
                callback,
                "Помощник выключен администратором. Ожидайте включения.",
                show_alert=True,
            )
            return
        current = settings_store.get_user_assistant_enabled(uid)
        settings_store.set_user_assistant_enabled(uid, not current)
        new_state = settings_store.get_user_assistant_enabled(uid)
        await _safe_answer(
            callback,
            "Помощник включён" if new_state else "Помощник выключен"
        )
        await _safe_edit(callback.message, _home_text(uid), get_main_menu_keyboard(uid))

    @router.callback_query(F.data == "menu_lib")
    async def cb_lib(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _lib_text(uid, 0), lib_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data.startswith("lib_page_"))
    async def cb_lib_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("lib_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(callback.message, _lib_text(uid, page), lib_keyboard(uid, page))
        await callback.answer()

    @router.callback_query(F.data.startswith("lib_show_"))
    async def cb_lib_show(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            lib_id = int(callback.data.replace("lib_show_", "", 1))
        except ValueError:
            await callback.answer("Текст не найден", show_alert=True)
            return
        item = settings_store.get_user_library_text(uid, lib_id)
        if item is None:
            await callback.answer("Текст не найден", show_alert=True)
            return
        text = item.get("text", "")
        for start in range(0, max(len(text), 1), 4000):
            await callback.bot.send_message(callback.message.chat.id, text[start : start + 4000])
        await callback.answer("Текст отправлен ниже — перешлите клиенту")

    @router.callback_query(F.data.startswith("lib_del_"))
    async def cb_lib_del(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            lib_id = int(callback.data.replace("lib_del_", "", 1))
        except ValueError:
            await callback.answer("Текст не найден", show_alert=True)
            return
        removed = settings_store.remove_user_library_text(uid, lib_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        items = settings_store.get_user_library(uid)
        _, page, _ = _page_slice(items, 0)
        await _safe_edit(callback.message, _lib_text(uid, page), lib_keyboard(uid, page))

    @router.callback_query(F.data == "lib_add")
    async def cb_lib_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_lib_title)
        await _safe_edit(
            callback.message,
            "📚 <b>Новый текст справочника</b>\n\n"
            "Сначала отправьте <b>название</b> — как подписать текст в списке.\n"
            "Например: <i>Инструкция по оплате</i>",
            get_back_keyboard("menu_lib"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_lib_title)
    async def process_lib_title(message: Message, state: FSMContext):
        uid = message.from_user.id
        title = (message.text or "").strip()
        if not title:
            await message.answer("Название не может быть пустым. Отправьте название:")
            return
        await state.update_data(lib_title=title)
        await state.set_state(SettingsStates.waiting_for_lib_text)
        await message.answer(
            f"Название: <i>«{html.escape(title)}»</i>\n\n"
            "Теперь отправьте <b>сам текст</b> — сохраню его в «Справочник».",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("menu_lib"),
        )

    @router.message(SettingsStates.waiting_for_lib_text)
    async def process_lib_text(message: Message, state: FSMContext):
        uid = message.from_user.id
        text = (message.text or "").strip()
        if not text:
            await message.answer("Текст не может быть пустым. Отправьте текст:")
            return
        data = await state.get_data()
        title = data.get("lib_title", "")
        settings_store.add_user_library_text(uid, title, text)
        await state.clear()
        await message.answer(
            "✅ Текст сохранён в «Справочник».",
            reply_markup=lib_keyboard(uid, 0),
        )

    @router.callback_query(F.data == "menu_words")
    async def cb_words(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _words_text(), words_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_start_msg")
    async def cb_start_msg(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _start_msg_text(uid, 0), start_msg_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data == "menu_end_msg")
    async def cb_end_msg(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _end_msg_text(uid, 0), end_msg_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data.startswith("greet_page_"))
    async def cb_greet_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("greet_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message, _start_msg_text(uid, page), start_msg_keyboard(uid, page)
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("thanks_page_"))
    async def cb_thanks_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("thanks_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message, _end_msg_text(uid, page), end_msg_keyboard(uid, page)
        )
        await callback.answer()

    @router.callback_query(F.data == "greet_add")
    async def cb_greet_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_greet_phrase)
        await _safe_edit(
            callback.message,
            "👋 <b>Новый Start msg</b>\n\n"
            "Отправьте <b>фразу-приветствие</b> клиента.\n"
            "Например: <i>Добрый день</i>",
            get_back_keyboard("menu_start_msg"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_greet_phrase)
    async def process_greet_phrase(message: Message, state: FSMContext):
        uid = message.from_user.id
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Текст не может быть пустым. Отправьте фразу-приветствие:")
            return
        await state.update_data(phrase=phrase)
        await state.set_state(SettingsStates.waiting_for_greet_reply)
        await message.answer(
            "Теперь отправьте <b>ответ</b>, который уйдёт на это приветствие.\n"
            "Например: <i>Добрый день!</i>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("menu_start_msg"),
        )

    @router.message(SettingsStates.waiting_for_greet_reply)
    async def process_greet_reply(message: Message, state: FSMContext):
        uid = message.from_user.id
        reply = (message.text or "").strip()
        if not reply:
            await message.answer("Текст не может быть пустым. Отправьте ответ:")
            return
        data = await state.get_data()
        phrase = data.get("phrase", "")
        settings_store.add_user_greeting(uid, phrase, reply)
        await state.clear()
        await message.answer(
            "✅ Start msg сохранён.\n\n"
            f"«{html.escape(phrase)}» → <i>«{html.escape(reply)}»</i>",
            parse_mode="HTML",
            reply_markup=start_msg_keyboard(uid, 0),
        )

    @router.callback_query(F.data == "thanks_add")
    async def cb_thanks_add(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_thanks)
        await _safe_edit(
            callback.message,
            "🙏 <b>Новый End msg</b>\n\n"
            "Отправьте <b>слово благодарности</b> — на него робот отвечать не будет.\n"
            "Например: <i>Рахмат</i>",
            get_back_keyboard("menu_end_msg"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_thanks)
    async def process_thanks(message: Message, state: FSMContext):
        uid = message.from_user.id
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Текст не может быть пустым. Отправьте слово благодарности:")
            return
        settings_store.add_user_thanks(uid, phrase)
        await state.clear()
        await message.answer(
            f"✅ End msg сохранён: <i>«{html.escape(phrase)}»</i>",
            parse_mode="HTML",
            reply_markup=end_msg_keyboard(uid, 0),
        )

    @router.callback_query(F.data.startswith("del_greet_"))
    async def cb_del_greet(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            greeting_id = int(callback.data.replace("del_greet_", "", 1))
        except ValueError:
            await callback.answer("Некорректное слово", show_alert=True)
            return
        removed = settings_store.remove_user_greeting(uid, greeting_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        await _safe_edit(
            callback.message, _start_msg_text(uid, 0), start_msg_keyboard(uid, 0)
        )

    @router.callback_query(F.data.startswith("del_thanks_"))
    async def cb_del_thanks(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            thanks_id = int(callback.data.replace("del_thanks_", "", 1))
        except ValueError:
            await callback.answer("Некорректное слово", show_alert=True)
            return
        removed = settings_store.remove_user_thanks(uid, thanks_id)
        await callback.answer("Удалено" if removed else "Не найдено")
        await _safe_edit(
            callback.message, _end_msg_text(uid, 0), end_msg_keyboard(uid, 0)
        )

    @router.callback_query(F.data == "thanks_reply")
    async def cb_thanks_reply(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_thanks_reply)
        reply = (settings_store.get_user_thanks_reply(uid) or "").strip()
        shown = f"«{html.escape(reply)}»" if reply else "не задан — робот молчит"
        await _safe_edit(
            callback.message,
            "✍️ <b>Ответ на благодарность</b>\n\n"
            f"Сейчас:\n<i>{shown}</i>\n\n"
            "Отправьте новый текст. Его получит клиент, приславший слово из End msg.",
            get_back_keyboard("menu_end_msg"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_thanks_reply)
    async def process_thanks_reply(message: Message, state: FSMContext):
        uid = message.from_user.id
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Текст не может быть пустым. Попробуйте ещё раз:")
            return
        settings_store.set_user_thanks_reply(uid, new_text)
        await state.clear()
        await message.answer(
            f"✅ Ответ на благодарность изменён:\n\n<i>«{html.escape(new_text)}»</i>",
            parse_mode="HTML",
            reply_markup=end_msg_keyboard(uid, 0),
        )

    @router.callback_query(F.data == "menu_no_action")
    async def cb_no_action_phrases(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(
            callback.message,
            _no_action_phrase_text(uid, 0),
            no_action_phrase_keyboard(uid, 0),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("no_action_phrase_page_"))
    async def cb_no_action_phrase_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("no_action_phrase_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message,
            _no_action_phrase_text(uid, page),
            no_action_phrase_keyboard(uid, page),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu_add_no_action_phrase")
    async def cb_add_no_action_phrase(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_no_action_phrase)
        await _safe_edit(
            callback.message,
            "✍️ <b>Добавить фразу без ответа</b>\n\n"
            "Отправьте фразу, на которую бот не будет реагировать.\n"
            "Например: <i>понятно</i>, <i>ясно</i>, <i>ок</i>",
            get_back_keyboard("menu_no_action"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_no_action_phrase)
    async def process_no_action_phrase(message: Message, state: FSMContext):
        uid = message.from_user.id
        phrase = (message.text or "").strip()
        if not phrase:
            await message.answer("Фраза не может быть пустой. Попробуйте ещё раз:")
            return
        settings_store.add_user_no_action_phrase(uid, phrase)
        await state.clear()
        await message.answer(
            f"✅ Фраза добавлена:\n\n<i>«{html.escape(phrase)}»</i>",
            parse_mode="HTML",
            reply_markup=no_action_phrase_keyboard(uid, 0),
        )

    @router.callback_query(F.data.startswith("del_no_action_phrase_"))
    async def cb_del_no_action_phrase(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            phrase_id = int(callback.data.replace("del_no_action_phrase_", "", 1))
        except ValueError:
            await callback.answer("Некорректный ID", show_alert=True)
            return
        removed = settings_store.remove_user_no_action_phrase(uid, phrase_id)
        await callback.answer("Удалено" if removed else "Фраза не найдена")
        await _safe_edit(
            callback.message,
            _no_action_phrase_text(uid, 0),
            no_action_phrase_keyboard(uid, 0),
        )

    @router.callback_query(F.data == "menu_edit_reply")
    async def cb_edit_reply(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.set_state(SettingsStates.waiting_for_scenario_reply)
        await _safe_edit(
            callback.message,
            "✍️ <b>Ответ по умолчанию</b>\n\n"
            f"Сейчас:\n<i>«{settings_store.scenario_reply}»</i>\n\n"
            "Отправьте новый текст. Он уйдёт, если ни один шаблон не подошёл.",
            get_back_keyboard("menu_messages"),
        )
        await callback.answer()

    @router.message(SettingsStates.waiting_for_scenario_reply)
    async def process_new_reply(message: Message, state: FSMContext):
        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Текст не может быть пустым. Попробуйте ещё раз:")
            return
        settings_store.scenario_reply = new_text
        await state.clear()
        uid = message.from_user.id
        await message.answer(
            f"✅ Ответ по умолчанию изменён:\n\n<i>«{html.escape(new_text)}»</i>",
            reply_markup=messages_keyboard(uid, 0),
            parse_mode="HTML",
        )

    # --- Авторизация ---

    @router.callback_query(F.data == "menu_auth")
    async def cb_auth(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        client = await _get_client_for_user(uid)
        is_auth = False
        try:
            if client and client.is_connected():
                is_auth = await client.is_user_authorized()
        except Exception:
            pass

        await state.clear()

        if is_auth:
            text = (
                "🔑 <b>Авторизация</b>\n\n"
                "🟢 Личный аккаунт авторизован, сессия сохранена. Вход больше не требуется."
            )
            kb = get_back_keyboard()
        else:
            text = (
                "🔑 <b>Авторизация</b>\n\n"
                "🔴 Аккаунт не авторизован.\n\n"
                "Нажмите «Войти по номеру телефона» — код придёт в Telegram или SMS. "
                "Код можно ввести кнопками или отправить сообщением."
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📱 Войти по номеру телефона",
                            callback_data="start_phone_auth",
                        )
                    ],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_assistant")],
                ]
            )

        await _safe_edit(callback.message, text, kb)
        await callback.answer()

    @router.callback_query(F.data == "start_phone_auth")
    async def cb_start_phone_auth(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        client: TelegramClient = router.telethon_client_getter() if hasattr(router, "telethon_client_getter") else None
        if client is None:
            # Если глобальный Telethon не настроен в .env, перенаправляем на персональную настройку
            await cb_start_setup(callback, state)
            return

        await callback.answer()
        await state.clear()
        await state.set_state(AuthStates.waiting_for_phone)
        await _safe_edit(
            callback.message,
            "📱 <b>Вход по номеру телефона</b>\n\n"
            "Отправьте номер вашего личного аккаунта (в формате +998...):",
            get_back_keyboard("menu_auth"),
        )

    @router.message(AuthStates.waiting_for_phone)
    async def process_phone(message: Message, state: FSMContext):
        uid = message.from_user.id
        raw_phone = (message.text or "").strip()
        digits = re.sub(r"\D", "", raw_phone)
        if not digits or len(digits) < 9:
            await message.answer(
                "Номер должен быть с кодом страны, например: +998901234567. Отправьте номер ещё раз:"
            )
            return
        phone = f"+{digits}"

        client: TelegramClient = router.telethon_client_getter() if hasattr(router, "telethon_client_getter") else None
        if client is None:
            await message.answer("❌ Telethon-клиент не запущен.", reply_markup=get_back_keyboard("menu_auth"))
            return

        try:
            if not client.is_connected():
                await client.connect()
            sent_code = await client.send_code_request(phone)
        except FloodWaitError as exc:
            await message.answer(
                f"❌ Telegram просит подождать {exc.seconds} сек. и отправить номер снова."
            )
            return
        except Exception as exc:
            logger.exception("Failed to send login code")
            await message.answer(f"❌ Не удалось отправить код: {exc}\nОтправьте номер ещё раз:")
            return

        code_length = getattr(sent_code.type, "length", 5)
        await state.update_data(
            phone=phone,
            phone_code_hash=sent_code.phone_code_hash,
            code_length=code_length,
            code_buffer="",
        )
        await state.set_state(AuthStates.waiting_for_code)

        if isinstance(sent_code.type, SentCodeTypeApp):
            dest = "в ваше <b>приложение Telegram</b> (в чат «Служебные уведомления» / «Telegram»)"
        elif isinstance(sent_code.type, SentCodeTypeSms):
            dest = "по <b>SMS</b> на ваш номер"
        else:
            dest = "в <b>Telegram</b> («Служебные уведомления»)"

        msg_text = (
            f"✅ <b>Код подтверждения отправлен {dest}.</b>\n"
            f"🔢 Длина кода: <b>{code_length} цифр</b>.\n\n"
            f"{_format_code_prompt('', code_length, title='Код из Telegram')}"
        )
        sent_msg = await message.answer(
            msg_text,
            reply_markup=_code_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(prompt_msg_id=sent_msg.message_id)

    @router.message(AuthStates.waiting_for_code)
    async def process_auth_code_text(message: Message, state: FSMContext):
        raw_text = (message.text or "").strip()
        digits = re.sub(r"\D", "", raw_text)
        try:
            await message.delete()
        except Exception:
            pass
        if not digits:
            await message.answer("Пожалуйста, введите цифры кода кнопками или сообщением:", reply_markup=_code_keyboard())
            return

        data = await state.get_data()
        code_length = data.get("code_length", len(digits))
        prompt_msg_id = data.get("prompt_msg_id")
        await state.update_data(code_buffer=digits)

        if prompt_msg_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=prompt_msg_id,
                    text=_format_code_prompt(digits, code_length, is_checking=True, title="Код из Telegram"),
                    reply_markup=_code_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            msg = await message.answer(
                _format_code_prompt(digits, code_length, is_checking=True, title="Код из Telegram"),
                reply_markup=_code_keyboard(),
                parse_mode="HTML",
            )
            await state.update_data(prompt_msg_id=msg.message_id)

        await _submit_code(message, digits, state)

    @router.callback_query(F.data.startswith("auth_"), AuthStates.waiting_for_code)
    async def cb_auth_code(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if callback.data == "auth_cancel":
            await state.clear()
            await callback.answer("Вход отменён")
            await _safe_edit(
                callback.message,
                "Вход отменён. Начать заново: «🔑 Авторизация».",
                get_back_keyboard("menu_auth"),
            )
            return

        if callback.data == "auth_resend":
            data = await state.get_data()
            phone = data.get("phone", "")
            client = router.telethon_client_getter() if hasattr(router, "telethon_client_getter") else None
            if not client or not phone:
                await callback.answer("Ошибка повторного запроса", show_alert=True)
                return
            try:
                if not client.is_connected():
                    await client.connect()
                sent_code = await client.send_code_request(phone)
                code_length = getattr(sent_code.type, "length", 5)
                await state.update_data(
                    phone_code_hash=sent_code.phone_code_hash,
                    code_length=code_length,
                    code_buffer="",
                )
                await callback.answer("Новый код отправлен")
                await _safe_edit(
                    callback.message,
                    f"✅ <b>Новый код отправлен в Telegram («Служебные уведомления»).</b>\n"
                    f"🔢 Длина кода: <b>{code_length} цифр</b>.\n\n"
                    f"{_format_code_prompt('', code_length, title='Код из Telegram')}",
                    _code_keyboard(),
                )
            except Exception as e:
                await callback.answer(f"Ошибка: {e}", show_alert=True)
            return

        data = await state.get_data()
        buffer = data.get("code_buffer", "")
        code_length = data.get("code_length", 5)

        if callback.data == "auth_del":
            buffer = buffer[:-1]
        elif callback.data == "auth_ok":
            if len(buffer) < code_length:
                await callback.answer(f"Введите все {code_length} цифр кода", show_alert=True)
                return
            await callback.answer()
            await state.update_data(code_buffer=buffer)
            await _safe_edit(
                callback.message,
                _format_code_prompt(buffer, code_length, is_checking=True, title="Код из Telegram"),
                _code_keyboard(),
            )
            await _submit_code(callback.message, buffer, state)
            return
        elif callback.data.startswith("auth_digit:"):
            digit = callback.data.split(":", 1)[1]
            if len(buffer) < code_length:
                buffer += digit
        else:
            await callback.answer()
            return

        await state.update_data(code_buffer=buffer)
        await callback.answer()

        if len(buffer) == code_length:
            await _safe_edit(
                callback.message,
                _format_code_prompt(buffer, code_length, is_checking=True, title="Код из Telegram"),
                _code_keyboard(),
            )
            await _submit_code(callback.message, buffer, state)
        else:
            await _safe_edit(
                callback.message,
                _format_code_prompt(buffer, code_length, is_checking=False, title="Код из Telegram"),
                _code_keyboard(),
            )

    @router.message(AuthStates.waiting_for_password)
    async def process_password(message: Message, state: FSMContext):
        uid = message.from_user.id
        password = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        if not password:
            await message.answer("Сообщение пустое. Отправьте пароль ещё раз:")
            return

        client: TelegramClient = router.telethon_client_getter()
        try:
            await client.sign_in(password=password)
        except Exception as exc:
            logger.exception("Failed to sign in with password")
            await message.answer(f"❌ Пароль не принят: {exc}\nОтправьте пароль ещё раз:")
            return

        creds = settings_store.get_user_credentials(uid)
        if creds:
            settings_store.save_user_credentials(
                user_id=uid,
                phone=creds.get("phone", ""),
                api_id=creds.get("api_id", 0),
                api_hash=creds.get("api_hash", ""),
                activated=True,
            )

        await state.clear()
        await message.answer(
            "🎉 <b>Авторизация успешна!</b> Сессия сохранена — "
            "при следующих запусках вход не потребуется.",
            reply_markup=get_main_menu_keyboard(uid),
            parse_mode="HTML",
        )

    # --- Исключения ---

    @router.callback_query(F.data == "menu_excluded")
    async def cb_excluded(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await state.clear()
        await _safe_edit(callback.message, _excluded_text(uid, 0), excluded_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data.startswith("excluded_page_"))
    async def cb_excluded_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("excluded_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message, _excluded_text(uid, page), excluded_keyboard(uid, page)
        )
        await callback.answer()

    @router.callback_query(F.data == "menu_add_excluded")
    async def cb_add_excluded(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        await callback.answer("Загружаю контакты...")
        try:
            client = await _get_client_for_user(uid)
            if not client or not client.is_connected():
                await _safe_edit(
                    callback.message,
                    "❌ Личный аккаунт Telegram не подключен.",
                    get_back_keyboard("menu_excluded"),
                )
                return
            contacts = await _load_dialogs(client)
            _contact_cache[uid] = [item for item in contacts if item[2] == "client"]
            await _safe_edit(
                callback.message,
                "🚫 <b>Добавить в исключения</b>\n\n"
                "Выберите контакт из списка:",
                excluded_contacts_keyboard(uid, 0),
            )
        except Exception as exc:
            logger.exception("Failed to load contacts")
            await _safe_edit(
                callback.message,
                f"❌ Ошибка: {exc}",
                get_back_keyboard("menu_excluded"),
            )

    @router.callback_query(F.data.startswith("excluded_contact_"))
    async def cb_excluded_contact(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            excluded_user_id = int(callback.data.replace("excluded_contact_", "", 1))
        except ValueError:
            await callback.answer("Некорректный пользователь", show_alert=True)
            return
        contact_cache = _get_contact_cache(uid)
        match = next((item for item in contact_cache if item[0] == excluded_user_id), None)
        if match is None:
            await callback.answer("Контакт не найден", show_alert=True)
            return
        name = match[1]
        settings_store.add_user_excluded_user(uid, excluded_user_id, "", name)
        await callback.answer(f"Добавлен: {name}")
        excluded_ids = set(int(eid) for eid in settings_store.get_user_excluded_users(uid).keys())
        available = [item for item in contact_cache if item[0] not in excluded_ids]
        try:
            page = available.index(match) // PAGE_SIZE
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message,
            "🚫 <b>Добавить в исключения</b>\n\nВыберите контакт из списка:",
            excluded_contacts_keyboard(uid, page),
        )

    @router.callback_query(F.data.startswith("excl_contact_page_"))
    async def cb_excl_contact_page(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            page = int(callback.data.replace("excl_contact_page_", "", 1))
        except ValueError:
            page = 0
        await _safe_edit(
            callback.message,
            "🚫 <b>Добавить в исключения</b>\n\nВыберите контакт из списка:",
            excluded_contacts_keyboard(uid, page),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("del_excluded_"))
    async def cb_del_excluded(callback: CallbackQuery):
        uid = callback.from_user.id
        try:
            excluded_user_id = int(callback.data.replace("del_excluded_", "", 1))
        except ValueError:
            await callback.answer("Некорректный пользователь", show_alert=True)
            return
        removed = settings_store.remove_user_excluded_user(uid, excluded_user_id)
        await callback.answer("Удалено" if removed else "Пользователь не найден")
        items = list(settings_store.get_user_excluded_users(uid).items())
        _, page, _ = _page_slice(items, 0)
        await _safe_edit(
            callback.message, _excluded_text(uid, page), excluded_keyboard(uid, page)
        )

    # --- Фильтры ---

    @router.callback_query(F.data == "chats_filter_groups")
    async def cb_chats_filter_groups(callback: CallbackQuery):
        uid = callback.from_user.id
        _chats_filter[uid] = "groups" if _get_chats_filter(uid) != "groups" else "all"
        await _safe_edit(callback.message, _chats_text(uid, 0), chats_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data == "chats_filter_contacts")
    async def cb_chats_filter_contacts(callback: CallbackQuery):
        uid = callback.from_user.id
        _chats_filter[uid] = "contacts" if _get_chats_filter(uid) != "contacts" else "all"
        await _safe_edit(callback.message, _chats_text(uid, 0), chats_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data == "add_filter_groups")
    async def cb_add_filter_groups(callback: CallbackQuery):
        uid = callback.from_user.id
        _add_filter[uid] = "groups" if _get_add_filter(uid) != "groups" else "all"
        await _safe_edit(callback.message, _add_chats_text(uid, 0), add_chats_keyboard(uid, 0))
        await callback.answer()

    @router.callback_query(F.data == "add_filter_contacts")
    async def cb_add_filter_contacts(callback: CallbackQuery):
        uid = callback.from_user.id
        _add_filter[uid] = "contacts" if _get_add_filter(uid) != "contacts" else "all"
        await _safe_edit(callback.message, _add_chats_text(uid, 0), add_chats_keyboard(uid, 0))
        await callback.answer()

    # --- Выход / Настройка ---

    @router.callback_query(F.data == "menu_logout")
    async def cb_logout(callback: CallbackQuery):
        uid = callback.from_user.id
        if uid == config.admin_user_id:
            await callback.answer("Админ не может выйти", show_alert=True)
            return
        settings_store.deactivate_user(uid)
        await callback.answer("Вы вышли")
        await _safe_edit(callback.message, _home_text(uid), get_main_menu_keyboard(uid))

    @router.callback_query(F.data == "menu_start_setup")
    async def cb_start_setup(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        client = _pending_auth_clients.pop(uid, None)
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass
        _clean_user_session(uid)
        await state.set_state(SettingsStates.waiting_for_user_creds)
        await state.update_data(setup_step="phone")
        phone_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await _safe_edit(
            callback.message,
            "📱 <b>Настройка помощника</b>\n\n"
            "Нажмите кнопку ниже, чтобы отправить номер вашего Telegram-аккаунта (или введите номер в формате +998...):",
        )
        await callback.message.answer("Нажмите кнопку или отправьте номер сообщением:", reply_markup=phone_kb)
        await callback.answer()

    async def _request_and_send_code(
        bot: Bot,
        chat_id: int,
        uid: int,
        phone: str,
        api_id: int,
        api_hash: str,
        state: FSMContext,
        edit_msg_id: int | None = None,
    ) -> bool:
        # 1. Отключаем предыдущий клиент, если был
        existing_client = _pending_auth_clients.pop(uid, None)
        if existing_client:
            try:
                if existing_client.is_connected():
                    await existing_client.disconnect()
            except Exception:
                pass

        # 2. Очищаем старые/поврежденные session-файлы
        _clean_user_session(uid)

        # 3. Инициализируем клиент с реальными десктопными метаданными
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

        try:
            await client.connect()
            sent_code = await client.send_code_request(phone)
        except FloodWaitError as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            _clean_user_session(uid)
            msg_text = (
                f"❌ Telegram временно ограничил запросы кодов.\n"
                f"Пожалуйста, подождите {exc.seconds} сек. и повторите попытку."
            )
            await _safe_edit_or_answer(bot, chat_id, edit_msg_id, msg_text, get_back_keyboard("menu_assistant"))
            return False
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            _clean_user_session(uid)
            logger.exception("Failed to send code to user %s", uid)
            error_str = str(exc)
            error_name = type(exc).__name__

            if isinstance(exc, ApiIdInvalidError) or "api_id/api_hash" in error_str.lower() or "ApiIdInvalid" in error_str:
                msg_text = (
                    "❌ <b>API_ID или API_HASH неверные.</b>\n\n"
                    "Получите их на <a href=\"https://my.telegram.org\">my.telegram.org</a>:\n"
                    "1. Войдите с вашим номером телефона\n"
                    "2. Перейдите в «API development tools»\n"
                    "3. Создайте приложение (если ещё нет)\n"
                    "4. Скопируйте <b>api_id</b> (число) и <b>api_hash</b> (строка)\n\n"
                    "Нажмите «◀️ Назад» и попробуйте снова."
                )
            elif "PhoneNumberInvalid" in error_name or "phone number is invalid" in error_str.lower():
                msg_text = f"❌ Неверный номер телефона (<code>{phone}</code>). Проверьте номер с кодом страны."
            elif "PhoneNumberBanned" in error_name:
                msg_text = "❌ Этот номер телефона заблокирован в Telegram."
            else:
                msg_text = f"❌ Не удалось отправить код: {html.escape(error_str)}\n\nПопробуйте ещё раз позже или проверьте данные."

            await _safe_edit_or_answer(bot, chat_id, edit_msg_id, msg_text, get_back_keyboard("menu_assistant"))
            return False

        # Сохраняем активный подключенный клиент в памяти
        _pending_auth_clients[uid] = client

        code_length = getattr(sent_code.type, "length", 5)
        if isinstance(sent_code.type, SentCodeTypeApp):
            delivery_info = (
                "📍 Код отправлен <b>в ваше приложение Telegram</b>!\n"
                "Проверьте чат <b>«Служебные уведомления» / «Telegram»</b> на вашем телефоне или компьютере."
            )
        elif isinstance(sent_code.type, SentCodeTypeSms):
            delivery_info = "📍 Код отправлен <b>по SMS</b> на ваш номер телефона."
        elif isinstance(sent_code.type, (SentCodeTypeCall, SentCodeTypeFlashCall)):
            delivery_info = "📍 Вам поступит <b>телефонный звонок</b> с кодом."
        else:
            delivery_info = "📍 Код отправлен в <b>Telegram</b> («Служебные уведомления»)."

        await state.set_state(SettingsStates.waiting_for_sms_code)
        await state.update_data(
            temp_client_session=f"session_{uid}",
            phone_code_hash=sent_code.phone_code_hash,
            code_length=code_length,
            code_buffer="",
        )

        msg_text = (
            "✅ <b>Код подтверждения отправлен!</b>\n\n"
            f"{delivery_info}\n\n"
            f"🔢 Длина кода: <b>{code_length} цифр</b>.\n\n"
            "💡 Введите код <b>кнопками ниже</b> или <b>отправьте сообщением в этот чат</b>:"
        )

        if edit_msg_id:
            try:
                await bot.edit_message_text(
                    text=msg_text,
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    reply_markup=_code_keyboard(),
                    parse_mode="HTML",
                )
                await state.update_data(prompt_msg_id=edit_msg_id)
                return True
            except Exception:
                pass

        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=msg_text,
            reply_markup=_code_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(prompt_msg_id=sent_msg.message_id)
        return True

    @router.message(SettingsStates.waiting_for_user_creds)
    async def process_user_creds(message: Message, state: FSMContext):
        uid = message.from_user.id
        data = await state.get_data()
        step = data.get("setup_step", "phone")

        if step == "phone":
            if message.contact:
                raw_phone = message.contact.phone_number
            else:
                raw_phone = (message.text or "").strip()

            if not raw_phone:
                await message.answer("Отправьте номер кнопкой ниже или напишите в чат:")
                return

            digits = re.sub(r"\D", "", raw_phone)
            if not digits or len(digits) < 9:
                await message.answer(
                    "Некорректный номер. Номер должен содержать код страны (например, +998901234567). Отправьте ещё раз:"
                )
                return
            phone_clean = f"+{digits}"

            if settings_store.is_phone_used_by_other(phone_clean, exclude_user_id=uid):
                await message.answer("Этот номер уже используется другим аккаунтом. Отправьте свой номер:")
                return

            await state.update_data(phone=phone_clean, setup_step="api_id", phone_msg_id=message.message_id)
            await message.answer(
                "Теперь отправьте <b>TELEGRAM_API_ID</b> (число):\n"
                "Узнать можно на <a href=\"https://my.telegram.org\">my.telegram.org</a>",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        elif step == "api_id":
            text = (message.text or "").strip()
            digits = re.sub(r"\D", "", text)
            try:
                api_id = int(digits)
            except ValueError:
                await message.answer("API_ID должен быть числом. Отправьте число:")
                return
            if api_id <= 0:
                await message.answer("API_ID должен быть положительным числом. Отправьте число:")
                return

            await state.update_data(api_id=api_id, setup_step="api_hash", api_id_msg_id=message.message_id)
            phone_msg_id = data.get("phone_msg_id")
            if phone_msg_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_msg_id)
                except Exception:
                    pass
            await message.answer(
                "Отправьте <b>TELEGRAM_API_HASH</b>:\n"
                "Узнать можно на <a href=\"https://my.telegram.org\">my.telegram.org</a>",
                parse_mode="HTML",
                reply_markup=get_back_keyboard("menu_assistant"),
            )
            return

        elif step == "api_hash":
            api_hash_val = (message.text or "").strip()
            if not api_hash_val or len(api_hash_val) < 10:
                await message.answer("API_HASH не может быть пустым. Отправьте API_HASH:")
                return

            await state.update_data(api_hash=api_hash_val, api_hash_msg_id=message.message_id)
            data = await state.get_data()
            api_id_val = data.get("api_id", 0)
            phone_val = data.get("phone", "")

            api_id_msg_id = data.get("api_id_msg_id")
            if api_id_msg_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=api_id_msg_id)
                except Exception:
                    pass

            loading_msg = await message.answer("⏳ Подключаюсь к Telegram и запрашиваю код подтверждения...")
            await _request_and_send_code(
                bot=message.bot,
                chat_id=message.chat.id,
                uid=uid,
                phone=phone_val,
                api_id=api_id_val,
                api_hash=api_hash_val,
                state=state,
                edit_msg_id=loading_msg.message_id,
            )
            try:
                await message.delete()
            except Exception:
                pass

    @router.message(SettingsStates.waiting_for_sms_code)
    async def process_sms_code_text(message: Message, state: FSMContext):
        uid = message.from_user.id
        raw_text = (message.text or "").strip()
        digits = re.sub(r"\D", "", raw_text)
        try:
            await message.delete()
        except Exception:
            pass
        if not digits:
            await message.answer(
                "Пожалуйста, введите цифры кода кнопками или сообщением:",
                reply_markup=_code_keyboard(),
            )
            return
        await _submit_sms_code(message, digits, state, uid)

    @router.callback_query(F.data.startswith("auth_"), SettingsStates.waiting_for_sms_code)
    async def cb_sms_code(callback: CallbackQuery, state: FSMContext):
        uid = callback.from_user.id
        if callback.data == "auth_cancel":
            client = _pending_auth_clients.pop(uid, None)
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    pass
            _clean_user_session(uid)
            await state.clear()
            await callback.answer("Настройка отменена")
            await _safe_edit(
                callback.message,
                "Настройка отменена. Начать заново: «🤖 Помощник».",
                get_back_keyboard("menu_assistant"),
            )
            return

        if callback.data == "auth_resend":
            data = await state.get_data()
            phone = data.get("phone", "")
            api_id = data.get("api_id", 0)
            api_hash = data.get("api_hash", "")
            if not phone or not api_id or not api_hash:
                await callback.answer("Данные устарели, начните настройку заново", show_alert=True)
                return
            await callback.answer("Отправляю новый код...")
            await _request_and_send_code(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                uid=uid,
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                state=state,
                edit_msg_id=callback.message.message_id,
            )
            return

        data = await state.get_data()
        buffer = data.get("code_buffer", "")
        code_length = data.get("code_length", 5)

        if callback.data == "auth_del":
            buffer = buffer[:-1]
        elif callback.data == "auth_ok":
            if len(buffer) < code_length:
                await callback.answer(f"Введите все {code_length} цифр кода", show_alert=True)
                return
            await callback.answer()
            await state.update_data(code_buffer=buffer)
            await _submit_sms_code(callback.message, buffer, state, uid)
            return
        elif callback.data.startswith("auth_digit:"):
            digit = callback.data.split(":", 1)[1]
            if len(buffer) < code_length:
                buffer += digit
        else:
            await callback.answer()
            return

        await state.update_data(code_buffer=buffer)
        await callback.answer()

        if len(buffer) == code_length:
            await _submit_sms_code(callback.message, buffer, state, uid)
        else:
            display_code = " ".join(buffer) + " " + " ".join("•" for _ in range(code_length - len(buffer)))
            await _safe_edit(
                callback.message,
                f"Код подтверждения (вводите кнопками или сообщением):\n<b>{display_code.strip()}</b>\n"
                f"<i>Осталось ввести: {code_length - len(buffer)} цифр</i>",
                _code_keyboard(),
            )

    async def _submit_sms_code(message: Message, code: str, state: FSMContext, user_id: int):
        data = await state.get_data()
        phone = data.get("phone", "")
        api_id = data.get("api_id", 0)
        api_hash = data.get("api_hash", "")
        session_name = data.get("temp_client_session", f"session_{user_id}")
        phone_code_hash = data.get("phone_code_hash", "")

        temp_client = _pending_auth_clients.get(user_id)
        if not temp_client or not temp_client.is_connected():
            temp_client = TelegramClient(
                session_name,
                api_id,
                api_hash,
                device_model="Desktop",
                system_version="Windows 10",
                app_version="4.16.8 x64",
                lang_code="ru",
                system_lang_code="ru",
            )
            try:
                await temp_client.connect()
                _pending_auth_clients[user_id] = temp_client
            except Exception as exc:
                logger.exception("Failed to connect client for code submit")
                prompt_text = f"❌ Ошибка подключения: {exc}"
                if message.from_user.is_bot:
                    await _safe_edit(message, prompt_text, get_back_keyboard("menu_assistant"))
                else:
                    await message.answer(prompt_text, reply_markup=get_back_keyboard("menu_assistant"))
                return

        try:
            await temp_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            await state.set_state(SettingsStates.waiting_for_password)
            prompt_text = (
                "🔒 <b>На аккаунте включён облачный пароль (2FA).</b>\n\n"
                "Отправьте ваш пароль следующим сообщением.\n\n"
                "<i>Сообщение с паролем бот сразу удалит для безопасности.</i>"
            )
            if message.from_user.is_bot:
                await _safe_edit(message, prompt_text, get_back_keyboard("menu_assistant"))
            else:
                await message.answer(prompt_text, parse_mode="HTML", reply_markup=get_back_keyboard("menu_assistant"))
            return
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            await state.update_data(code_buffer="")
            err_detail = "истёк" if isinstance(exc, PhoneCodeExpiredError) else "неверный"
            prompt_text = (
                f"❌ <b>Код {err_detail}.</b>\n\n"
                "Введите код заново кнопками или сообщением, либо нажмите «🔄 Запросить код повторно»:"
            )
            if message.from_user.is_bot:
                await _safe_edit(message, prompt_text, _code_keyboard())
            else:
                await message.answer(prompt_text, parse_mode="HTML", reply_markup=_code_keyboard())
            return
        except Exception as exc:
            logger.exception("Failed to sign in with code")
            await state.update_data(code_buffer="")
            prompt_text = (
                f"❌ Ошибка входа: {html.escape(str(exc))}\n\n"
                "Введите код заново кнопками или сообщением:"
            )
            if message.from_user.is_bot:
                await _safe_edit(message, prompt_text, _code_keyboard())
            else:
                await message.answer(prompt_text, parse_mode="HTML", reply_markup=_code_keyboard())
            return

        # Успешный вход — закрываем временное подключение
        client = _pending_auth_clients.pop(user_id, None)
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass

        await _finalize_setup(message, state, user_id)

    @router.message(SettingsStates.waiting_for_password)
    async def process_user_password(message: Message, state: FSMContext):
        uid = message.from_user.id
        password = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        if not password:
            await message.answer("Сообщение пустое. Отправьте пароль ещё раз:")
            return

        data = await state.get_data()
        phone = data.get("phone", "")
        api_id = data.get("api_id", 0)
        api_hash = data.get("api_hash", "")
        session_name = data.get("temp_client_session", f"session_{uid}")

        temp_client = _pending_auth_clients.get(uid)
        if not temp_client or not temp_client.is_connected():
            temp_client = TelegramClient(
                session_name,
                api_id,
                api_hash,
                device_model="Desktop",
                system_version="Windows 10",
                app_version="4.16.8 x64",
                lang_code="ru",
                system_lang_code="ru",
            )
            try:
                await temp_client.connect()
                _pending_auth_clients[uid] = temp_client
            except Exception as exc:
                await message.answer(f"❌ Ошибка подключения: {exc}")
                return

        try:
            await temp_client.sign_in(password=password)
        except Exception as exc:
            logger.exception("Failed to sign in with password")
            await message.answer(f"❌ Пароль не принят: {exc}\nОтправьте пароль ещё раз:")
            return

        client = _pending_auth_clients.pop(uid, None)
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass

        await _finalize_setup(message, state, uid)

    async def _finalize_setup(message: Message, state: FSMContext, user_id: int):
        data = await state.get_data()
        phone = data.get("phone", "")
        api_id = data.get("api_id", 0)
        api_hash = data.get("api_hash", "")
        settings_store.save_user_credentials(
            user_id=user_id,
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            activated=True,
        )
        await state.clear()

        personal_msg_ids = [
            data.get("phone_msg_id"),
            data.get("api_id_msg_id"),
            data.get("api_hash_msg_id"),
            data.get("prompt_msg_id"),
        ]
        for msg_id in personal_msg_ids:
            if msg_id:
                try:
                    await message.bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
                except Exception:
                    pass

        success_text = (
            "🎉 <b>Авторизация успешна! Настройка завершена.</b>\n\n"
            "Ваш Telegram-аккаунт успешно подключен! Теперь вы можете настраивать группы и автоответы."
        )
        if message.from_user.is_bot:
            await _safe_edit(message, success_text, _assistant_keyboard_for_user(user_id))
        else:
            await message.answer(
                success_text,
                parse_mode="HTML",
                reply_markup=_assistant_keyboard_for_user(user_id),
            )

    dp.include_router(router)


_active_bot: Bot | None = None
_active_dp: Dispatcher | None = None
_polling_task: asyncio.Task | None = None


async def start_admin_bot(telethon_client_getter) -> None:
    global _active_bot, _active_dp, _polling_task

    token = settings_store.get_bot_token() or config.admin_bot_token
    if not token:
        logger.error("Admin bot token is not set. Admin bot will not start.")
        return

    logger.info("=" * 60)
    logger.info("ADMIN BOT: Starting polling...")
    logger.info("=" * 60)
    _active_bot = Bot(token=token)
    _active_dp = Dispatcher()
    register_admin_handlers(_active_dp, telethon_client_getter)

    logger.info("Starting Admin Bot...")
    try:
        await _active_dp.start_polling(_active_bot, allowed_updates=_active_dp.resolve_used_update_types())
    except Exception as exc:
        logger.error("Admin bot stopped with error: %s", exc)


async def restart_admin_bot(telethon_client_getter) -> str:
    global _active_bot, _active_dp, _polling_task

    token = settings_store.get_bot_token() or config.admin_bot_token
    if not token:
        return "Токен не задан"

    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except (asyncio.CancelledError, Exception):
            pass
    if _active_bot:
        try:
            await _active_bot.session.close()
        except Exception:
            pass

    _active_bot = Bot(token=token)
    _active_dp = Dispatcher()
    register_admin_handlers(_active_dp, telethon_client_getter)

    _polling_task = asyncio.create_task(
        _active_dp.start_polling(_active_bot, allowed_updates=_active_dp.resolve_used_update_types())
    )
    logger.info("Admin bot restarted with new token")
    return "Бот перезапущен"
