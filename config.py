"""
Загрузка и валидация конфигурации из .env
Ничего из этого файла не должно попадать в логи (см. main.py / logging setup).
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
from storage.settings import settings_store

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return int(val)


def _get_chat_ids(name: str) -> set[int]:
    raw = os.getenv(name, "") or ""
    ids = set()

    for part in raw.split(","):
        part = part.strip()

        if not part:
            continue

        try:
            ids.add(int(part))
        except ValueError:
            pass

    return ids


@dataclass
class Config:
    # Telegram personal account (Telethon)
    telegram_api_id: int = field(
        default_factory=lambda: _get_int("TELEGRAM_API_ID", 0)
    )

    telegram_api_hash: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_API_HASH", "")
    )

    telegram_session_name: str = field(
        default_factory=lambda: os.getenv(
            "TELEGRAM_SESSION_NAME",
            "telegram_session"
        )
    )

    telegram_phone: str = field(
        default_factory=lambda: os.getenv(
            "TELEGRAM_PHONE",
            ""
        ).strip()
    )

    # Admin Bot (Telegram Bot API)
    admin_bot_token: str = field(
        default_factory=lambda: os.getenv(
            "ADMIN_BOT_TOKEN",
            ""
        ).strip()
    )

    @property
    def admin_user_id(self) -> int:
        return settings_store.get_admin_user_id()

    # Behavior & Scenario
    auto_reply: bool = field(
        default_factory=lambda: _get_bool(
            "AUTO_REPLY",
            True
        )
    )

    reply_delay: float = field(
        default_factory=lambda: float(
            _get_int("REPLY_DELAY", 1)
        )
    )

    batch_window: float = field(
        default_factory=lambda: float(
            _get_int("BATCH_WINDOW", 120)
        )
    )

    # AI conversation context
    context_window: int = field(
        default_factory=lambda: _get_int(
            "CONTEXT_WINDOW",
            10
        )
    )

    # Web admin panel
    web_admin_login: str = field(
        default_factory=lambda: os.getenv("WEB_ADMIN_LOGIN", "admin")
    )

    web_admin_password: str = field(
        default_factory=lambda: os.getenv("WEB_ADMIN_PASSWORD", "admin")
    )

    web_secret: str = field(
        default_factory=lambda: os.getenv("WEB_SECRET", "change-me-in-env")
    )

    web_port: int = field(
        default_factory=lambda: _get_int("WEB_PORT", 8000)
    )

    web_app_base_url: str = field(
        default_factory=lambda: os.getenv("WEB_APP_BASE_URL", "").strip().rstrip("/")
    )

    bot_username: str = field(
        default_factory=lambda: os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    )

    @property
    def allowed_chat_ids(self) -> set[int]:
        # Получаем из settings_store (управляется через админ-бот)
        # либо если там пусто, fallback на ALLOWED_CHAT_IDS из .env
        # для обратной совместимости
        store_chats = settings_store.get_allowed_chat_ids()

        if store_chats:
            return store_chats

        return _get_chat_ids("ALLOWED_CHAT_IDS")

    @property
    def scenario_reply(self) -> str:
        return settings_store.scenario_reply

    def validate(self) -> None:
        import logging
        missing = []

        if not self.telegram_api_id:
            missing.append("TELEGRAM_API_ID")

        if not self.telegram_api_hash:
            missing.append("TELEGRAM_API_HASH")

        if not self.admin_bot_token:
            missing.append("ADMIN_BOT_TOKEN")

        if missing:
            logging.getLogger("config").warning(
                "Не заполнены переменные в .env: %s. "
                "Заполните через веб-админку или .env",
                ", ".join(missing),
            )


config = Config()