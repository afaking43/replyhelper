"""
Контекст диалога и защита от повторной обработки — всё в памяти процесса.
Для первой версии этого достаточно (см. ТЗ п.10, п.13); при желании
можно позже подменить на Redis/SQLite, не меняя интерфейс класса.
"""
from collections import deque, defaultdict
from dataclasses import dataclass, field


@dataclass
class ChatContext:
    history: deque = field(default_factory=lambda: deque(maxlen=1))
    processed_ids: set = field(default_factory=set)


class ContextStore:
    def __init__(self, window: int):
        self._window = window
        self._chats: dict[tuple[int, int], ChatContext] = {}

    def _get(self, chat_id: int, user_id: int = 0) -> ChatContext:
        key = (user_id, chat_id)
        chat = self._chats.get(key)
        if chat is None:
            chat = ChatContext(history=deque(maxlen=self._window))
            self._chats[key] = chat
        return chat

    def get_history(self, chat_id: int, user_id: int = 0) -> list[dict]:
        return list(self._get(chat_id, user_id).history)

    def add_user_message(self, chat_id: int, text: str, user_id: int = 0) -> None:
        self._get(chat_id, user_id).history.append({"role": "user", "content": text})

    def add_assistant_message(self, chat_id: int, text: str, user_id: int = 0) -> None:
        self._get(chat_id, user_id).history.append({"role": "assistant", "content": text})

    def is_processed(self, chat_id: int, message_id: int, user_id: int = 0) -> bool:
        return message_id in self._get(chat_id, user_id).processed_ids

    def mark_processed(self, chat_id: int, message_id: int, user_id: int = 0) -> None:
        chat = self._get(chat_id, user_id)
        chat.processed_ids.add(message_id)
        # не даём множеству расти бесконечно
        if len(chat.processed_ids) > 2000:
            chat.processed_ids = set(list(chat.processed_ids)[-1000:])
