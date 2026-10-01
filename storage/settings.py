"""
Хранилище настроек на SQLite (WAL) + файлы credentials + .env для bot_token.
"""
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("storage")

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
CREDENTIALS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "credentials")
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

DEFAULT_GREETINGS = [
    {"phrase": "доброе утро", "reply": "Доброе утро!"},
    {"phrase": "добрый день", "reply": "Добрый день!"},
    {"phrase": "добрый вечер", "reply": "Добрый вечер!"},
    {"phrase": "доброй ночи", "reply": "Доброй ночи!"},
    {"phrase": "здравствуйте", "reply": "Здравствуйте!"},
    {"phrase": "здравствуй", "reply": "Здравствуйте!"},
    {"phrase": "здраствуйте", "reply": "Здравствуйте!"},
    {"phrase": "привет", "reply": "Здравствуйте!"},
    {"phrase": "приветствую", "reply": "Здравствуйте!"},
    {"phrase": "салом", "reply": "Ва алейкум ассалом!"},
    {"phrase": "ассалому алейкум", "reply": "Ва алейкум ассалом!"},
    {"phrase": "ассалому алекум", "reply": "Ва алейкум ассалом!"},
    {"phrase": "assalomu aleykum", "reply": "Va alaykum assalom!"},
    {"phrase": "assalomu alaykum", "reply": "Va alaykum assalom!"},
    {"phrase": "asalomu aleykum", "reply": "Va alaykum assalom!"},
    {"phrase": "salom", "reply": "Va alaykum assalom!"},
    {"phrase": "salom alaykum", "reply": "Va alaykum assalom!"},
]

DEFAULT_THANKS = [
    "спасибо", "спасибо большое", "большое спасибо", "спасибо огромное",
    "огромное спасибо", "благодарю", "спасибо вам", "спасибо за ответ",
    "спс", "спасиб", "пасибо", "пасиб", "благодарочка",
    "рахмат", "рахмат вам", "рахмат сизга", "катта рахмат",
    "коп рахмат", "рахмат ака",
    "rahmat", "raxmat", "rahmad", "raxmad", "rahmat sizga",
    "katta rahmat", "rahmat sizlarga", "tashakkur", "ташаккур",
]

_PUNCT_RE = re.compile(r"[^\w\s]")


def _plain(text: str) -> str:
    value = _PUNCT_RE.sub(" ", (text or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", value).strip()


def _norm(text: str) -> str:
    value = (text or "").strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", value)


def _read_env_value(key: str) -> str:
    try:
        if not os.path.exists(ENV_FILE):
            return ""
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _write_env_value(key: str, value: str) -> None:
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


_thread_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id INTEGER PRIMARY KEY,
    phone TEXT,
    api_id INTEGER,
    api_hash TEXT,
    activated INTEGER DEFAULT 0,
    last_activity TEXT
);

CREATE TABLE IF NOT EXISTS user_chats (
    user_id INTEGER,
    chat_id INTEGER,
    title TEXT,
    chat_type TEXT,
    enabled INTEGER DEFAULT 1,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS user_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    question TEXT,
    answer TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_templates_uid ON user_templates(user_id);

CREATE TABLE IF NOT EXISTS user_greetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phrase TEXT,
    reply TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_greetings_uid ON user_greetings(user_id);

CREATE TABLE IF NOT EXISTS user_thanks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phrase TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_thanks_uid ON user_thanks(user_id);

CREATE TABLE IF NOT EXISTS user_thanks_reply (
    user_id INTEGER PRIMARY KEY,
    reply TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_library_uid ON user_library(user_id);

CREATE TABLE IF NOT EXISTS user_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_id INTEGER,
    chat_title TEXT,
    client TEXT,
    kind TEXT,
    request TEXT,
    reply TEXT,
    status TEXT DEFAULT 'sent'
);
CREATE INDEX IF NOT EXISTS idx_user_tasks_uid ON user_tasks(user_id);

CREATE TABLE IF NOT EXISTS user_excluded (
    user_id INTEGER,
    excluded_user_id INTEGER,
    username TEXT,
    name TEXT,
    PRIMARY KEY (user_id, excluded_user_id)
);

CREATE TABLE IF NOT EXISTS user_no_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phrase TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_no_action_uid ON user_no_action(user_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY,
    assistant_enabled INTEGER DEFAULT 1,
    last_menu_message_id INTEGER
);

CREATE TABLE IF NOT EXISTS standard_greetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT,
    reply TEXT
);

CREATE TABLE IF NOT EXISTS standard_thanks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT
);

CREATE TABLE IF NOT EXISTS standard_no_action (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT
);

CREATE TABLE IF NOT EXISTS analytics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    user_id INTEGER,
    ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_analytics_ts ON analytics(ts);
CREATE INDEX IF NOT EXISTS idx_analytics_uid ON analytics(user_id);

CREATE TABLE IF NOT EXISTS error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    module TEXT,
    message TEXT,
    details TEXT,
    user_id INTEGER,
    chat_id INTEGER,
    ts TEXT,
    status TEXT DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_error_logs_ts ON error_logs(ts);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    name TEXT,
    username TEXT,
    source TEXT,
    status TEXT DEFAULT 'active',
    subscribed_at TEXT,
    last_activity TEXT
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _thread_local.conn = conn
    return conn


def _init_db() -> None:
    conn = _get_conn()
    conn.executescript(_SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM standard_greetings").fetchone()[0] == 0:
        for g in DEFAULT_GREETINGS:
            conn.execute("INSERT INTO standard_greetings (phrase, reply) VALUES (?, ?)",
                         (g["phrase"], g["reply"]))
    if conn.execute("SELECT COUNT(*) FROM standard_thanks").fetchone()[0] == 0:
        for phrase in DEFAULT_THANKS:
            conn.execute("INSERT INTO standard_thanks (phrase) VALUES (?)", (phrase,))
    if conn.execute("SELECT COUNT(*) FROM global_settings WHERE key='scenario_reply'").fetchone()[0] == 0:
        conn.execute("INSERT INTO global_settings (key, value) VALUES ('scenario_reply', ?)",
                     ("Принято, взята в работу.",))
    conn.commit()


class SettingsStore:
    def __init__(self, filepath: str = DB_PATH):
        self.filepath = filepath
        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        _init_db()
        self._migrate_json_if_needed()

    @property
    def _data(self) -> dict:
        return _CompatDict(self)

    def _migrate_json_if_needed(self) -> None:
        json_file = os.path.join(os.path.dirname(__file__), "settings.json")
        if not os.path.exists(json_file):
            return
        conn = _get_conn()
        if conn.execute("SELECT COUNT(*) FROM user_credentials").fetchone()[0] > 0:
            return
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.loads(f.read().strip() or "{}")
        except Exception as exc:
            logger.warning("Failed to read old settings.json for migration: %s", exc)
            return

        sr = data.get("scenario_reply", "Принято, взята в работу.")
        conn.execute("UPDATE global_settings SET value=? WHERE key='scenario_reply'", (sr,))

        for uid_str, creds in data.get("user_credentials", {}).items():
            uid = int(uid_str)
            conn.execute(
                "INSERT OR REPLACE INTO user_credentials (user_id, phone, api_id, api_hash, activated, last_activity) VALUES (?,?,?,?,?,?)",
                (uid, creds.get("phone", ""), int(creds.get("api_id", 0)),
                 creds.get("api_hash", ""), int(creds.get("activated", False)),
                 creds.get("last_activity")),
            )
            cred_path = os.path.join(CREDENTIALS_DIR, f"{uid}.json")
            os.makedirs(os.path.dirname(cred_path), exist_ok=True)
            with open(cred_path, "w", encoding="utf-8") as cf:
                json.dump(creds, cf, ensure_ascii=False, indent=2)

        for uid_str, udata in data.get("user_data", {}).items():
            uid = int(uid_str)
            for cid_str, info in udata.get("allowed_chats", {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO user_chats (user_id, chat_id, title, chat_type, enabled) VALUES (?,?,?,?,?)",
                    (uid, int(cid_str), info.get("title", ""), info.get("type", "group"), int(info.get("enabled", True))),
                )
            for t in udata.get("templates", []):
                conn.execute("INSERT INTO user_templates (user_id, question, answer) VALUES (?,?,?)",
                             (uid, t.get("question", ""), t.get("answer", "")))
            for g in udata.get("greetings", []):
                conn.execute("INSERT INTO user_greetings (user_id, phrase, reply) VALUES (?,?,?)",
                             (uid, g.get("phrase", ""), g.get("reply", "")))
            for th in udata.get("thanks", []):
                conn.execute("INSERT INTO user_thanks (user_id, phrase) VALUES (?,?)",
                             (uid, th.get("phrase", "")))
            tr = udata.get("thanks_reply", "")
            if tr:
                conn.execute("INSERT OR REPLACE INTO user_thanks_reply (user_id, reply) VALUES (?,?)", (uid, tr))
            for lib in udata.get("library", []):
                conn.execute("INSERT INTO user_library (user_id, title, text) VALUES (?,?,?)",
                             (uid, lib.get("title", ""), lib.get("text", "")))
            for task in udata.get("tasks", []):
                conn.execute(
                    "INSERT INTO user_tasks (user_id, chat_id, chat_title, client, kind, request, reply, status) VALUES (?,?,?,?,?,?,?,?)",
                    (uid, task.get("chat_id", 0), task.get("chat_title", ""), task.get("client", ""),
                     task.get("kind", ""), task.get("request", ""), task.get("reply", ""), task.get("status", "sent")),
                )
            for eid_str, einfo in udata.get("excluded_users", {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO user_excluded (user_id, excluded_user_id, username, name) VALUES (?,?,?,?)",
                    (uid, int(eid_str), einfo.get("username", ""), einfo.get("name", "")),
                )
            for nap in udata.get("no_action_phrases", []):
                conn.execute("INSERT INTO user_no_action (user_id, phrase) VALUES (?,?)",
                             (uid, nap.get("phrase", "")))
            us = conn.execute("SELECT user_id FROM user_settings WHERE user_id=?", (uid,)).fetchone()
            if us is None:
                conn.execute("INSERT INTO user_settings (user_id, assistant_enabled) VALUES (?,?)",
                             (uid, int(udata.get("assistant_enabled", True))))

        for g in data.get("standard_greetings", []):
            if not conn.execute("SELECT 1 FROM standard_greetings WHERE phrase=?", (g.get("phrase", ""),)).fetchone():
                conn.execute("INSERT INTO standard_greetings (phrase, reply) VALUES (?,?)",
                             (g.get("phrase", ""), g.get("reply", "")))
        for th in data.get("standard_thanks", []):
            if not conn.execute("SELECT 1 FROM standard_thanks WHERE phrase=?", (th.get("phrase", ""),)).fetchone():
                conn.execute("INSERT INTO standard_thanks (phrase) VALUES (?)", (th.get("phrase", ""),))
        for nap in data.get("standard_no_action_phrases", []):
            if not conn.execute("SELECT 1 FROM standard_no_action WHERE phrase=?", (nap.get("phrase", ""),)).fetchone():
                conn.execute("INSERT INTO standard_no_action (phrase) VALUES (?)", (nap.get("phrase", ""),))

        for ev in data.get("analytics", []):
            conn.execute("INSERT INTO analytics (event_type, user_id, ts) VALUES (?,?,?)",
                         (ev.get("type", ""), ev.get("user_id"), ev.get("ts", "")))
        for log in data.get("error_logs", []):
            conn.execute(
                "INSERT INTO error_logs (level, module, message, details, user_id, chat_id, ts, status) VALUES (?,?,?,?,?,?,?,?)",
                (log.get("level", ""), log.get("module", ""), log.get("message", ""),
                 log.get("details", ""), log.get("user_id"), log.get("chat_id"),
                 log.get("ts", ""), log.get("status", "new")),
            )
        for sub in data.get("subscribers", []):
            conn.execute(
                "INSERT OR IGNORE INTO subscribers (telegram_id, name, username, source, status, subscribed_at, last_activity) VALUES (?,?,?,?,?,?,?)",
                (sub.get("telegram_id"), sub.get("name", ""), sub.get("username", ""),
                 sub.get("source", ""), sub.get("status", "active"),
                 sub.get("subscribed_at", ""), sub.get("last_activity", "")),
            )

        bt = data.get("bot_token", "")
        if bt:
            _write_env_value("ADMIN_BOT_TOKEN", bt)

        conn.commit()
        logger.info("Migrated settings.json to SQLite successfully")

    def load(self) -> None:
        pass

    def save(self) -> None:
        pass

    @property
    def scenario_reply(self) -> str:
        row = _get_conn().execute("SELECT value FROM global_settings WHERE key='scenario_reply'").fetchone()
        return row[0] if row else "Принято, взята в работу."

    @scenario_reply.setter
    def scenario_reply(self, value: str) -> None:
        conn = _get_conn()
        conn.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('scenario_reply', ?)", (value,))
        conn.commit()

    def get_allowed_chat_ids(self) -> set[int]:
        return set()

    def get_all_chats(self) -> dict:
        return {}

    def get_watchlist(self) -> dict:
        return {}

    def add_chat(self, chat_id: int, title: str, chat_type: str = "group") -> None:
        pass

    def remove_chat(self, chat_id: int) -> bool:
        return False

    def get_templates(self) -> list[dict]:
        return []

    def add_template(self, question: str, answer: str) -> dict:
        return {}

    def remove_template(self, template_id: int) -> bool:
        return False

    def find_reply(self, text: str) -> str:
        return self.scenario_reply

    @property
    def assistant_enabled(self) -> bool:
        row = _get_conn().execute("SELECT value FROM global_settings WHERE key='assistant_enabled'").fetchone()
        if row is None:
            return True
        return row[0] == "1"

    @assistant_enabled.setter
    def assistant_enabled(self, value: bool) -> None:
        conn = _get_conn()
        conn.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('assistant_enabled', ?)",
                     ("1" if value else "0",))
        conn.commit()

    @property
    def thanks_reply(self) -> str:
        return ""

    @thanks_reply.setter
    def thanks_reply(self, value: str) -> None:
        pass

    def get_greetings(self) -> list[dict]:
        return []

    def add_greeting(self, phrase: str, reply: str) -> dict:
        return {}

    def remove_greeting(self, greeting_id: int) -> bool:
        return False

    def get_thanks(self) -> list[dict]:
        return []

    def add_thanks(self, phrase: str) -> dict:
        return {}

    def remove_thanks(self, thanks_id: int) -> bool:
        return False

    def get_library(self) -> list[dict]:
        return []

    def get_library_text(self, lib_id: int) -> dict | None:
        return None

    def add_library_text(self, title: str, text: str) -> dict:
        return {}

    def remove_library_text(self, lib_id: int) -> bool:
        return False

    def get_tasks(self, done: bool | None = None) -> list[dict]:
        return []

    def add_task(self, chat_id: int, chat_title: str, client: str, kind: str, request: str, reply: str) -> dict:
        return {}

    def complete_task(self, task_id: int) -> dict | None:
        return None

    def remove_task(self, task_id: int) -> dict | None:
        return None

    def clear_done_tasks(self) -> int:
        return 0

    def get_excluded_users(self) -> dict:
        return {}

    def add_excluded_user(self, user_id: int, username: str, name: str) -> dict:
        return {}

    def remove_excluded_user(self, user_id: int) -> bool:
        return False

    def is_user_excluded(self, user_id: int) -> bool:
        return False

    def _cred_file_path(self, user_id: int) -> str:
        return os.path.join(CREDENTIALS_DIR, f"{user_id}.json")

    def _read_cred_file(self, user_id: int) -> dict | None:
        path = self._cred_file_path(user_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.loads(f.read().strip() or "{}")
        except Exception:
            return None

    def _write_cred_file(self, user_id: int, creds: dict) -> None:
        path = self._cred_file_path(user_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)

    def _delete_cred_file(self, user_id: int) -> None:
        path = self._cred_file_path(user_id)
        try:
            os.remove(path)
        except OSError:
            pass

    def get_user_credentials(self, user_id: int) -> dict | None:
        file_creds = self._read_cred_file(user_id)
        if file_creds:
            return file_creds
        row = _get_conn().execute("SELECT * FROM user_credentials WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        creds = {
            "user_id": row["user_id"],
            "phone": row["phone"] or "",
            "api_id": row["api_id"] or 0,
            "api_hash": row["api_hash"] or "",
            "activated": bool(row["activated"]),
        }
        if row["last_activity"]:
            creds["last_activity"] = row["last_activity"]
        self._write_cred_file(user_id, creds)
        return creds

    def is_phone_used_by_other(self, phone: str, exclude_user_id: int = 0) -> bool:
        row = _get_conn().execute(
            "SELECT user_id FROM user_credentials WHERE phone=? AND user_id!=?",
            (phone, exclude_user_id),
        ).fetchone()
        return row is not None

    def save_user_credentials(
        self, user_id: int, phone: str, api_id: int, api_hash: str, activated: bool = False
    ) -> None:
        creds = {
            "user_id": user_id,
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "activated": activated,
        }
        existing = self._read_cred_file(user_id)
        if existing and existing.get("last_activity"):
            creds["last_activity"] = existing["last_activity"]
        self._write_cred_file(user_id, creds)
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO user_credentials (user_id, phone, api_id, api_hash, activated) VALUES (?,?,?,?,?)",
            (user_id, phone, api_id, api_hash, int(activated)),
        )
        conn.commit()

    def deactivate_user(self, user_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("UPDATE user_credentials SET activated=0 WHERE user_id=?", (user_id,))
        conn.commit()
        if cur.rowcount == 0:
            return False
        creds = self._read_cred_file(user_id)
        if creds:
            creds["activated"] = False
            self._write_cred_file(user_id, creds)
        return True

    def get_no_action_phrases(self) -> list[dict]:
        return []

    def add_no_action_phrase(self, phrase: str) -> dict:
        return {}

    def remove_no_action_phrase(self, phrase_id: int) -> bool:
        return False

    def is_no_action_phrase(self, text: str) -> bool:
        return False

    def get_last_menu_message_id(self, user_id: int) -> int | None:
        row = _get_conn().execute("SELECT last_menu_message_id FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def set_last_menu_message_id(self, user_id: int, message_id: int | None) -> None:
        conn = _get_conn()
        conn.execute("INSERT INTO user_settings (user_id, last_menu_message_id) VALUES (?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET last_menu_message_id=excluded.last_menu_message_id",
                     (user_id, message_id))
        conn.commit()

    def get_user_assistant_enabled(self, user_id: int) -> bool:
        row = _get_conn().execute("SELECT assistant_enabled FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return True
        return bool(row[0])

    def set_user_assistant_enabled(self, user_id: int, value: bool) -> None:
        conn = _get_conn()
        conn.execute("INSERT INTO user_settings (user_id, assistant_enabled) VALUES (?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET assistant_enabled=excluded.assistant_enabled",
                     (user_id, int(value)))
        conn.commit()

    def get_user_watchlist(self, user_id: int) -> dict:
        rows = _get_conn().execute(
            "SELECT chat_id, title, chat_type, enabled FROM user_chats WHERE user_id=? AND enabled=1",
            (user_id,),
        ).fetchall()
        return {str(r[0]): {"title": r[1], "type": r[2], "enabled": True} for r in rows}

    def get_user_allowed_chat_ids(self, user_id: int) -> set[int]:
        rows = _get_conn().execute(
            "SELECT chat_id FROM user_chats WHERE user_id=? AND enabled=1", (user_id,),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def add_user_chat(self, user_id: int, chat_id: int, title: str, chat_type: str = "group") -> None:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO user_chats (user_id, chat_id, title, chat_type, enabled) VALUES (?,?,?,?,1)",
            (user_id, chat_id, title, chat_type),
        )
        conn.commit()

    def remove_user_chat(self, user_id: int, chat_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_chats WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        conn.commit()
        return cur.rowcount > 0

    def get_user_templates(self, user_id: int) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, question, answer FROM user_templates WHERE user_id=?", (user_id,),
        ).fetchall()
        return [{"id": r[0], "question": r[1], "answer": r[2]} for r in rows]

    def add_user_template(self, user_id: int, question: str, answer: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO user_templates (user_id, question, answer) VALUES (?,?,?)",
                           (user_id, question.strip(), answer.strip()))
        conn.commit()
        return {"id": cur.lastrowid, "question": question.strip(), "answer": answer.strip()}

    def remove_user_template(self, user_id: int, template_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_templates WHERE id=? AND user_id=?", (template_id, user_id))
        conn.commit()
        return cur.rowcount > 0

    def update_user_template(self, user_id: int, template_id: int, question: str, answer: str) -> bool:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE user_templates SET question=?, answer=? WHERE id=? AND user_id=?",
            (question.strip(), answer.strip(), template_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def clear_user_templates(self, user_id: int) -> int:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_templates WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount

    def get_user_greetings(self, user_id: int) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, phrase, reply FROM user_greetings WHERE user_id=?", (user_id,),
        ).fetchall()
        return [{"id": r[0], "phrase": r[1], "reply": r[2]} for r in rows]

    def add_user_greeting(self, user_id: int, phrase: str, reply: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO user_greetings (user_id, phrase, reply) VALUES (?,?,?)",
                           (user_id, phrase.strip(), reply.strip()))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase.strip(), "reply": reply.strip()}

    def remove_user_greeting(self, user_id: int, greeting_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_greetings WHERE id=? AND user_id=?", (greeting_id, user_id))
        conn.commit()
        return cur.rowcount > 0

    def get_user_thanks(self, user_id: int) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, phrase FROM user_thanks WHERE user_id=?", (user_id,),
        ).fetchall()
        return [{"id": r[0], "phrase": r[1]} for r in rows]

    def add_user_thanks(self, user_id: int, phrase: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO user_thanks (user_id, phrase) VALUES (?,?)",
                           (user_id, phrase.strip()))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase.strip()}

    def remove_user_thanks(self, user_id: int, thanks_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_thanks WHERE id=? AND user_id=?", (thanks_id, user_id))
        conn.commit()
        return cur.rowcount > 0

    def get_user_thanks_reply(self, user_id: int) -> str:
        row = _get_conn().execute("SELECT reply FROM user_thanks_reply WHERE user_id=?", (user_id,)).fetchone()
        return str(row[0]) if row else ""

    def set_user_thanks_reply(self, user_id: int, value: str) -> None:
        conn = _get_conn()
        conn.execute("INSERT INTO user_thanks_reply (user_id, reply) VALUES (?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET reply=excluded.reply",
                     (user_id, value))
        conn.commit()

    def get_user_library(self, user_id: int) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, title, text FROM user_library WHERE user_id=?", (user_id,),
        ).fetchall()
        return [{"id": r[0], "title": r[1], "text": r[2]} for r in rows]

    def get_user_library_text(self, user_id: int, lib_id: int) -> dict | None:
        row = _get_conn().execute(
            "SELECT id, title, text FROM user_library WHERE id=? AND user_id=?", (lib_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "title": row[1], "text": row[2]}

    def add_user_library_text(self, user_id: int, title: str, text: str) -> dict:
        conn = _get_conn()
        clean_title = (title or "").strip()
        cur = conn.execute("INSERT INTO user_library (user_id, title, text) VALUES (?,?,?)",
                           (user_id, clean_title, (text or "").strip()))
        conn.commit()
        lib_id = cur.lastrowid
        return {"id": lib_id, "title": clean_title if clean_title else f"Текст {lib_id}", "text": (text or "").strip()}

    def remove_user_library_text(self, user_id: int, lib_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_library WHERE id=? AND user_id=?", (lib_id, user_id))
        conn.commit()
        return cur.rowcount > 0

    def get_user_tasks(self, user_id: int, done: bool | None = None) -> list[dict]:
        conn = _get_conn()
        if done is None:
            rows = conn.execute(
                "SELECT id, chat_id, chat_title, client, kind, request, reply, status FROM user_tasks WHERE user_id=?",
                (user_id,),
            ).fetchall()
        else:
            status = "done" if done else "sent"
            rows = conn.execute(
                "SELECT id, chat_id, chat_title, client, kind, request, reply, status FROM user_tasks WHERE user_id=? AND status=?",
                (user_id, status),
            ).fetchall()
        return [{"id": r[0], "chat_id": r[1], "chat_title": r[2], "client": r[3],
                 "kind": r[4], "request": r[5], "reply": r[6], "status": r[7]} for r in rows]

    def add_user_task(self, user_id: int, chat_id: int, chat_title: str, client: str, kind: str, request: str, reply: str) -> dict:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO user_tasks (user_id, chat_id, chat_title, client, kind, request, reply, status) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, chat_id, chat_title, client, kind, request, reply, "sent"),
        )
        conn.commit()
        return {"id": cur.lastrowid, "chat_id": chat_id, "chat_title": chat_title,
                "client": client, "kind": kind, "request": request, "reply": reply, "status": "sent"}

    def complete_user_task(self, user_id: int, task_id: int) -> dict | None:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, chat_id, chat_title, client, kind, request, reply, status FROM user_tasks WHERE id=? AND user_id=?",
            (task_id, user_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE user_tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
        return {"id": row[0], "chat_id": row[1], "chat_title": row[2], "client": row[3],
                "kind": row[4], "request": row[5], "reply": row[6], "status": "done"}

    def remove_user_task(self, user_id: int, task_id: int) -> dict | None:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, chat_id, chat_title, client, kind, request, reply, status FROM user_tasks WHERE id=? AND user_id=?",
            (task_id, user_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM user_tasks WHERE id=?", (task_id,))
        conn.commit()
        return {"id": row[0], "chat_id": row[1], "chat_title": row[2], "client": row[3],
                "kind": row[4], "request": row[5], "reply": row[6], "status": row[7]}

    def clear_user_done_tasks(self, user_id: int) -> int:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_tasks WHERE user_id=? AND status='done'", (user_id,))
        conn.commit()
        return cur.rowcount

    def clear_all_user_tasks(self, user_id: int) -> int:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_tasks WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount

    def get_user_excluded_users(self, user_id: int) -> dict:
        rows = _get_conn().execute(
            "SELECT excluded_user_id, username, name FROM user_excluded WHERE user_id=?", (user_id,),
        ).fetchall()
        return {str(r[0]): {"user_id": r[0], "username": r[1], "name": r[2]} for r in rows}

    def add_user_excluded_user(self, user_id: int, excluded_user_id: int, username: str, name: str) -> dict:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO user_excluded (user_id, excluded_user_id, username, name) VALUES (?,?,?,?)",
            (user_id, excluded_user_id, username or "", name or ""),
        )
        conn.commit()
        return {"user_id": excluded_user_id, "username": username or "", "name": name or ""}

    def remove_user_excluded_user(self, user_id: int, excluded_user_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_excluded WHERE user_id=? AND excluded_user_id=?",
                           (user_id, excluded_user_id))
        conn.commit()
        return cur.rowcount > 0

    def is_user_excluded_for(self, user_id: int, excluded_user_id: int) -> bool:
        row = _get_conn().execute(
            "SELECT 1 FROM user_excluded WHERE user_id=? AND excluded_user_id=?",
            (user_id, excluded_user_id),
        ).fetchone()
        return row is not None

    def get_user_no_action_phrases(self, user_id: int) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, phrase FROM user_no_action WHERE user_id=?", (user_id,),
        ).fetchall()
        return [{"id": r[0], "phrase": r[1]} for r in rows]

    def add_user_no_action_phrase(self, user_id: int, phrase: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO user_no_action (user_id, phrase) VALUES (?,?)",
                           (user_id, phrase))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase}

    def remove_user_no_action_phrase(self, user_id: int, phrase_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_no_action WHERE id=? AND user_id=?", (phrase_id, user_id))
        conn.commit()
        return cur.rowcount > 0

    def is_no_action_phrase_for(self, user_id: int, text: str) -> bool:
        key = _plain(text)
        conn = _get_conn()
        rows = conn.execute("SELECT phrase FROM user_no_action WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            if _plain(r[0]) == key:
                return True
        rows = conn.execute("SELECT phrase FROM standard_no_action").fetchall()
        for r in rows:
            if _plain(r[0]) == key:
                return True
        return False

    def find_user_reply(self, user_id: int, text: str) -> str:
        normalized = _norm(text)
        best_answer = None
        best_len = -1
        conn = _get_conn()
        rows = conn.execute("SELECT question, answer FROM user_templates WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            question = _norm(r[0])
            if not question:
                continue
            if question in normalized and len(question) > best_len:
                best_len = len(question)
                best_answer = r[1] or ""
        if best_answer:
            return best_answer
        return self.scenario_reply

    def user_greeting_reply(self, user_id: int, text: str) -> str | None:
        key = _plain(text)
        conn = _get_conn()
        row = conn.execute("SELECT reply FROM user_greetings WHERE user_id=? AND phrase=?",
                           (user_id, key)).fetchone()
        if row is not None:
            return row[0] or ""
        rows = conn.execute("SELECT phrase, reply FROM standard_greetings").fetchall()
        for r in rows:
            if _plain(r[0]) == key:
                return r[1] or ""
        return None

    def is_user_thanks(self, user_id: int, text: str) -> bool:
        key = _plain(text)
        conn = _get_conn()
        rows = conn.execute("SELECT phrase FROM user_thanks WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            if _plain(r[0]) == key:
                return True
        rows = conn.execute("SELECT phrase FROM standard_thanks").fetchall()
        for r in rows:
            if _plain(r[0]) == key:
                return True
        return False

    def clear_user_data(self, user_id: int) -> None:
        conn = _get_conn()
        for table in ("user_chats", "user_templates", "user_greetings", "user_thanks",
                       "user_library", "user_tasks", "user_excluded", "user_no_action"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_thanks_reply WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        conn.commit()

    def delete_user_account(self, user_id: int) -> None:
        conn = _get_conn()
        for table in ("user_chats", "user_templates", "user_greetings", "user_thanks",
                       "user_library", "user_tasks", "user_excluded", "user_no_action"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_thanks_reply WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_credentials WHERE user_id=?", (user_id,))
        conn.commit()
        self._delete_cred_file(user_id)

    def get_all_user_ids(self) -> list[int]:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM ("
            "  SELECT user_id FROM user_credentials"
            "  UNION"
            "  SELECT user_id FROM user_settings"
            "  UNION"
            "  SELECT user_id FROM user_chats"
            "  UNION"
            "  SELECT user_id FROM user_templates"
            "  UNION"
            "  SELECT user_id FROM user_tasks"
            ")"
        ).fetchall()
        return [r[0] for r in rows]

    def get_activated_credentials(self) -> list[dict]:
        rows = _get_conn().execute("SELECT * FROM user_credentials WHERE activated=1").fetchall()
        result = []
        for r in rows:
            creds = {
                "user_id": r["user_id"],
                "phone": r["phone"] or "",
                "api_id": r["api_id"] or 0,
                "api_hash": r["api_hash"] or "",
                "activated": True,
            }
            if r["last_activity"]:
                creds["last_activity"] = r["last_activity"]
            result.append(creds)
        return result

    def get_all_credentials(self) -> list[dict]:
        rows = _get_conn().execute("SELECT * FROM user_credentials").fetchall()
        result = []
        for r in rows:
            creds = {
                "user_id": r["user_id"],
                "phone": r["phone"] or "",
                "api_id": r["api_id"] or 0,
                "api_hash": r["api_hash"] or "",
                "activated": bool(r["activated"]),
            }
            if r["last_activity"]:
                creds["last_activity"] = r["last_activity"]
            result.append(creds)
        return result

    def get_standard_greetings(self) -> list[dict]:
        rows = _get_conn().execute("SELECT id, phrase, reply FROM standard_greetings").fetchall()
        return [{"id": r[0], "phrase": r[1], "reply": r[2]} for r in rows]

    def add_standard_greeting(self, phrase: str, reply: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO standard_greetings (phrase, reply) VALUES (?,?)",
                           (phrase.strip(), reply.strip()))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase.strip(), "reply": reply.strip()}

    def remove_standard_greeting(self, greeting_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM standard_greetings WHERE id=?", (greeting_id,))
        conn.commit()
        return cur.rowcount > 0

    def get_standard_thanks(self) -> list[dict]:
        rows = _get_conn().execute("SELECT id, phrase FROM standard_thanks").fetchall()
        return [{"id": r[0], "phrase": r[1]} for r in rows]

    def add_standard_thanks(self, phrase: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO standard_thanks (phrase) VALUES (?)", (phrase.strip(),))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase.strip()}

    def remove_standard_thanks(self, thanks_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM standard_thanks WHERE id=?", (thanks_id,))
        conn.commit()
        return cur.rowcount > 0

    def get_standard_no_action_phrases(self) -> list[dict]:
        rows = _get_conn().execute("SELECT id, phrase FROM standard_no_action").fetchall()
        return [{"id": r[0], "phrase": r[1]} for r in rows]

    def add_standard_no_action_phrase(self, phrase: str) -> dict:
        conn = _get_conn()
        cur = conn.execute("INSERT INTO standard_no_action (phrase) VALUES (?)", (phrase,))
        conn.commit()
        return {"id": cur.lastrowid, "phrase": phrase}

    def remove_standard_no_action_phrase(self, phrase_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM standard_no_action WHERE id=?", (phrase_id,))
        conn.commit()
        return cur.rowcount > 0

    def record_event(self, event_type: str, user_id: int | None = None) -> None:
        conn = _get_conn()
        conn.execute("INSERT INTO analytics (event_type, user_id, ts) VALUES (?,?,?)",
                     (event_type, user_id, datetime.now().isoformat()))
        conn.execute("DELETE FROM analytics WHERE id NOT IN (SELECT id FROM analytics ORDER BY id DESC LIMIT 5000)")
        conn.commit()

    def get_analytics(self, period_start: str | None = None, period_end: str | None = None) -> list[dict]:
        conn = _get_conn()
        query = "SELECT event_type, user_id, ts FROM analytics WHERE 1=1"
        params = []
        if period_start:
            query += " AND ts >= ?"
            params.append(period_start)
        if period_end:
            query += " AND ts <= ?"
            params.append(period_end)
        query += " ORDER BY id"
        rows = conn.execute(query, params).fetchall()
        return [{"type": r[0], "user_id": r[1], "ts": r[2]} for r in rows]

    def get_stats_summary(self, period_start: str | None = None) -> dict:
        events = self.get_analytics(period_start=period_start)
        return {
            "incoming": sum(1 for e in events if e.get("type") == "incoming"),
            "auto_replies": sum(1 for e in events if e.get("type") == "auto_reply"),
            "manual_replies": sum(1 for e in events if e.get("type") == "manual_reply"),
            "errors": sum(1 for e in events if e.get("type") == "error"),
            "new_users": sum(1 for e in events if e.get("type") == "user_activated"),
            "website_logins": sum(1 for e in events if e.get("type") in ("website_login", "website_visit")),
        }

    def add_error_log(
        self, level: str, module: str, message: str,
        details: str = "", user_id: int | None = None, chat_id: int | None = None,
    ) -> dict:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO error_logs (level, module, message, details, user_id, chat_id, ts, status) VALUES (?,?,?,?,?,?,?,?)",
            (level, module, message, details, user_id, chat_id, datetime.now().isoformat(), "new"),
        )
        conn.execute("DELETE FROM error_logs WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT 2000)")
        conn.commit()
        return {
            "id": cur.lastrowid, "level": level, "module": module, "message": message,
            "details": details, "user_id": user_id, "chat_id": chat_id,
            "ts": datetime.now().isoformat(), "status": "new",
        }

    def get_error_logs(self, level: str | None = None, module: str | None = None) -> list[dict]:
        conn = _get_conn()
        query = "SELECT id, level, module, message, details, user_id, chat_id, ts, status FROM error_logs WHERE 1=1"
        params = []
        if level:
            query += " AND level=?"
            params.append(level)
        if module:
            query += " AND module=?"
            params.append(module)
        query += " ORDER BY id DESC"
        rows = conn.execute(query, params).fetchall()
        return [{"id": r[0], "level": r[1], "module": r[2], "message": r[3],
                 "details": r[4], "user_id": r[5], "chat_id": r[6], "ts": r[7], "status": r[8]} for r in rows]

    def mark_error_resolved(self, log_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("UPDATE error_logs SET status='resolved' WHERE id=?", (log_id,))
        conn.commit()
        return cur.rowcount > 0

    def clear_old_error_logs(self, days: int = 30) -> int:
        conn = _get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cur = conn.execute("DELETE FROM error_logs WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount

    def get_subscribers(self) -> list[dict]:
        rows = _get_conn().execute(
            "SELECT id, telegram_id, name, username, source, status, subscribed_at, last_activity FROM subscribers ORDER BY id"
        ).fetchall()
        return [{"id": r[0], "telegram_id": r[1], "name": r[2], "username": r[3],
                 "source": r[4], "status": r[5], "subscribed_at": r[6], "last_activity": r[7]} for r in rows]

    def add_subscriber(
        self, telegram_id: int, name: str, username: str = "", source: str = ""
    ) -> dict:
        conn = _get_conn()
        existing = conn.execute("SELECT id FROM subscribers WHERE telegram_id=?", (telegram_id,)).fetchone()
        if existing:
            conn.execute("UPDATE subscribers SET last_activity=? WHERE telegram_id=?",
                         (datetime.now().isoformat(), telegram_id))
            conn.commit()
            row = conn.execute(
                "SELECT id, telegram_id, name, username, source, status, subscribed_at, last_activity FROM subscribers WHERE telegram_id=?",
                (telegram_id,),
            ).fetchone()
            return {"id": row[0], "telegram_id": row[1], "name": row[2], "username": row[3],
                    "source": row[4], "status": row[5], "subscribed_at": row[6], "last_activity": row[7]}
        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO subscribers (telegram_id, name, username, source, status, subscribed_at, last_activity) VALUES (?,?,?,?,?,?,?)",
            (telegram_id, name, username, source, "active", now, now),
        )
        conn.commit()
        return {"id": cur.lastrowid, "telegram_id": telegram_id, "name": name,
                "username": username, "source": source, "status": "active",
                "subscribed_at": now, "last_activity": now}

    def update_subscriber_status(self, telegram_id: int, status: str) -> bool:
        conn = _get_conn()
        cur = conn.execute("UPDATE subscribers SET status=? WHERE telegram_id=?", (status, telegram_id))
        conn.commit()
        return cur.rowcount > 0

    def remove_subscriber(self, telegram_id: int) -> bool:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM subscribers WHERE telegram_id=?", (telegram_id,))
        conn.commit()
        return cur.rowcount > 0

    def get_bot_token(self) -> str:
        return _read_env_value("ADMIN_BOT_TOKEN")

    def save_bot_token(self, token: str) -> None:
        _write_env_value("ADMIN_BOT_TOKEN", token.strip())

    def update_user_activity(self, user_id: int) -> None:
        conn = _get_conn()
        now = datetime.now().isoformat()
        conn.execute("UPDATE user_credentials SET last_activity=? WHERE user_id=?", (now, user_id))
        conn.commit()
        creds = self._read_cred_file(user_id)
        if creds:
            creds["last_activity"] = now
            self._write_cred_file(user_id, creds)

    def get_user_stats(self, user_id: int) -> dict:
        conn = _get_conn()
        rows = conn.execute("SELECT event_type FROM analytics WHERE user_id=?", (user_id,)).fetchall()
        return {
            "incoming": sum(1 for r in rows if r[0] == "incoming"),
            "auto_replies": sum(1 for r in rows if r[0] == "auto_reply"),
            "manual_replies": sum(1 for r in rows if r[0] == "manual_reply"),
        }

    def clear_user_analytics(self, user_id: int) -> None:
        conn = _get_conn()
        conn.execute("DELETE FROM analytics WHERE user_id=?", (user_id,))
        conn.commit()

    def clear_all_analytics(self) -> None:
        conn = _get_conn()
        conn.execute("DELETE FROM analytics")
        conn.commit()

    def clear_all_error_logs(self) -> None:
        conn = _get_conn()
        conn.execute("DELETE FROM error_logs")
        conn.commit()


class _CompatDict:
    def __init__(self, store: SettingsStore):
        self._store = store

    def get(self, key, default=None):
        if key == "analytics":
            return self._store.get_analytics()
        if key == "error_logs":
            return self._store.get_error_logs()
        return default


settings_store = SettingsStore()
