"""Обёртка над OpenAI API. Ключ никогда не логируется."""
import logging
from openai import AsyncOpenAI

from config import config
from ai.prompt import build_messages

logger = logging.getLogger("ai")

_client = AsyncOpenAI(api_key=config.openai_api_key)

NO_REPLY = "[NO_REPLY]"


async def generate_reply(history: list[dict], new_text: str) -> str | None:
    """
    Возвращает текст ответа, либо None если отвечать не нужно ([NO_REPLY]
    или ошибка модели).
    """
    messages = build_messages(history, new_text)

    logger.info("[AI] Generating response...")
    try:
        response = await _client.chat.completions.create(
            model=config.openai_model,
            messages=messages,
            temperature=0.4,
            max_tokens=300,
        )
    except Exception as exc:
        # Не роняем обработку сообщения из-за сбоя AI — просто не отвечаем.
        logger.error("[AI] OpenAI request failed: %s", exc)
        return None

    text = (response.choices[0].message.content or "").strip()

    if not text or text.upper().startswith(NO_REPLY):
        logger.info("[AI] NO_REPLY")
        return None

    logger.info("[AI] Response:\n%s", text)
    return text


async def generate_standard_greetings_ai(count: int = 5) -> list[dict[str, str]]:
    prompt = (
        f"Сгенерируй {count} популярных вариантов приветствий клиента и ответов бота на русском и узбекском языках.\n"
        "Формат ответа STRICTLY JSON массив объектов: [{\"phrase\": \"...\", \"reply\": \"...\"}]. Без ```json и без текста вокруг."
    )
    try:
        response = await _client.chat.completions.create(
            model=config.openai_model,
            messages=[
                {"role": "system", "content": "You output JSON arrays only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=500,
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        import json
        data = json.loads(content)
        if isinstance(data, list):
            res = []
            for item in data:
                p = str(item.get("phrase", "")).strip()
                r = str(item.get("reply", "")).strip()
                if p and r:
                    res.append({"phrase": p, "reply": r})
            return res
    except Exception as exc:
        logger.error("[AI] Failed to generate greetings: %s", exc)
    return []


async def generate_standard_thanks_ai(count: int = 5) -> list[str]:
    prompt = (
        f"Сгенерируй {count} популярных фраз благодарности (End msg) от клиентов на русском и узбекском языках.\n"
        "Формат ответа STRICTLY JSON массив строк: [\"фраза1\", \"фраза2\"]. Без ```json и без текста вокруг."
    )
    try:
        response = await _client.chat.completions.create(
            model=config.openai_model,
            messages=[
                {"role": "system", "content": "You output JSON arrays only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=400,
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        import json
        data = json.loads(content)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception as exc:
        logger.error("[AI] Failed to generate thanks: %s", exc)
    return []
