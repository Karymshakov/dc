"""Второе мнение модели: выбрать тему, когда поиск не уверен.

Правиловый поиск не знает слов, которых нет в базе и в синонимах:
«как в кз остаться на связи?» — это роуминг, но ни одного общего слова
с темой нет. В таких случаях бот показывает три кнопки. Этот модуль
даёт модели сделать то, что сделал бы абонент: нажать нужную кнопку.

Модель **не пишет ответ**. Ей дают вопрос и названия 15 тем и просят
вернуть одно число — номер темы или 0, если вопрос не по базе. Ответ
абоненту берётся из `faq.json` дословно, тем же путём, что и по нажатию
кнопки. Поэтому выдумать факт модель не может физически: её вывод —
число, и всё, что не является номером существующей темы, отбрасывается.

Границы:

* `answer()` модуль не вызывает никогда — контракт жюри работает без
  сети и ключей. Второе мнение есть только в Telegram-боте.
* Выключено по умолчанию, включается `LLM_ROUTER_ENABLED=1`.
* Нет ключа, нет сети, таймаут, исчерпан лимит, модель ответила не
  числом — на экране остаются три кнопки, как без модуля.

Транспорт, ключ и лимиты общие с `faq_bot.rephrase`: один выход в сеть
и один счётчик расходов на обе функции.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import rephrase
from .contracts import FaqEntry
from .speech import api_key, extract_text

logger = logging.getLogger(__name__)

__all__ = ["ENABLED", "Choice", "build_prompt", "choose", "parse"]

#: Включается осознанно, как и переформулировка: по умолчанию бот не
#: тратит чужие деньги и не зависит от сети.
ENABLED = rephrase.flag("LLM_ROUTER_ENABLED")

# Абонент уже видит три кнопки и может нажать сам, поэтому ждать модель
# долго незачем: не успела — остаются кнопки.
TIMEOUT_SECONDS = 6
MAX_QUESTION_CHARS = 200
MAX_OUTPUT_TOKENS = 8
NO_TOPIC = 0

# Ровно одно число и ничего больше. «Тема 4», «4, роуминг» и любые
# рассуждения отклоняются: формат задан жёстко, и отступление от него
# значит, что модель не поняла задание.
_NUMBER = re.compile(r"\s*(\d{1,3})\s*\.?\s*")

Transport = Callable[..., dict]


@dataclass(frozen=True)
class Choice:
    """Решение модели: номер темы или None с причиной отказа."""

    faq_id: int | None
    reason: str = ""


def build_prompt(question: str, entries: Sequence[FaqEntry]) -> str:
    """Вопрос и названия тем. Ответов и цен в промпте нет вовсе."""
    topics = "\n".join(
        f"{entry.id}. {entry.question_ru} / {entry.question_ky}"
        for entry in entries
    )
    return (
        "Ты сортируешь вопросы абонентов оператора мобильной связи по "
        "темам справочника. Вопрос может быть на русском или кыргызском, "
        "с опечатками, сленгом и латиницей.\n\n"
        f"Темы:\n{topics}\n\n"
        f"Вопрос абонента: {question.strip()[:MAX_QUESTION_CHARS]}\n\n"
        "Верни одно число: номер темы, на которую этот вопрос. Если вопрос "
        "не про мобильную связь, это просто приветствие или болтовня, или "
        f"ни одна тема не подходит — верни {NO_TOPIC}. Не угадывай. "
        "Только число, без слов."
    )


def parse(raw: str, valid_ids: Sequence[int]) -> int | None:
    """Номер существующей темы из ответа модели или None."""
    match = _NUMBER.fullmatch(raw or "")
    if match is None:
        return None
    number = int(match.group(1))
    return number if number in valid_ids else None


def _request_body(prompt: str) -> dict:
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "candidateCount": 1,
        },
    }


def choose(question: str, entries: Sequence[FaqEntry], *,
           chat_id: str | None = None,
           transport: Transport | None = None,
           model: str = rephrase.DEFAULT_MODEL) -> Choice:
    """Спросить у модели номер темы. Исключений не бросает."""
    if not question.strip() or not entries:
        return Choice(None, "нечего спрашивать")

    key = api_key()
    if not key:
        return Choice(None, "нет ключа GEMINI_API_KEY")

    allowed, limit_reason = rephrase.BUDGET.allows(chat_id)
    if not allowed:
        return Choice(None, limit_reason)

    # Списываем попытку до обращения к сети: см. CallBudget.record.
    rephrase.BUDGET.record(chat_id)
    send = transport or rephrase.transport
    try:
        payload = send(rephrase.ENDPOINT.format(model=model),
                       _request_body(build_prompt(question, entries)),
                       TIMEOUT_SECONDS, api_key=key)
    except Exception as error:                      # сеть, таймаут, разбор
        logger.warning("второе мнение: модель не ответила: %s", error)
        return Choice(None, f"модель недоступна: {error}")

    raw = extract_text(payload) if isinstance(payload, dict) else ""
    faq_id = parse(raw, [entry.id for entry in entries])
    if faq_id is None:
        return Choice(None, "модель не выбрала тему")
    return Choice(faq_id)
