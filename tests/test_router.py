"""Второе мнение модели: она выбирает тему, но не пишет ответ.

Сеть в тестах не используется: транспорт подменяется целиком.
"""
from __future__ import annotations

import asyncio

import pytest

from faq_bot import bot, rephrase, router
from faq_bot.answer import all_entries


def _payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _transport(text: str):
    def send(url, body, timeout, *, api_key=""):
        return _payload(text)
    return send


@pytest.fixture(autouse=True)
def _with_key(monkeypatch):
    monkeypatch.setattr(router, "api_key", lambda: "test-key")
    rephrase.reset()
    bot.reset_runtime_state()
    yield
    rephrase.reset()
    bot.reset_runtime_state()


# --- разбор ответа модели --------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("4", 4), (" 4 \n", 4), ("4.", 4), ("15", 15),
    ("0", None), ("99", None), ("", None),
    ("тема 4", None), ("4, роуминг", None), ("4 или 7", None),
    ("Думаю, это роуминг", None),
])
def test_parse_accepts_only_a_bare_existing_number(raw, expected):
    ids = [entry.id for entry in all_entries()]
    assert router.parse(raw, ids) == expected


def test_prompt_carries_topics_but_no_answers():
    """Модели не из чего взять факт: в промпте только названия тем."""
    entries = all_entries()
    prompt = router.build_prompt("как в кз остаться на связи?", entries)

    for entry in entries:
        assert entry.question_ru in prompt
        assert entry.answer_ru not in prompt
        assert entry.answer_ky not in prompt


# --- выбор темы ------------------------------------------------------------

def test_choose_returns_topic_named_by_model():
    choice = router.choose("как в кз остаться на связи?", all_entries(),
                           transport=_transport("4"))
    assert choice.faq_id == 4


@pytest.mark.parametrize("raw", ["0", "тема 4", "99", "не знаю"])
def test_choose_rejects_everything_that_is_not_a_topic_number(raw):
    choice = router.choose("как ты?", all_entries(), transport=_transport(raw))
    assert choice.faq_id is None
    assert choice.reason


def test_choose_survives_network_failure():
    def broken(*args, **kwargs):
        raise TimeoutError("нет сети")

    choice = router.choose("адм", all_entries(), transport=broken)
    assert choice.faq_id is None
    assert "недоступна" in choice.reason


def test_choose_without_key_never_calls_the_network(monkeypatch):
    monkeypatch.setattr(router, "api_key", lambda: "")
    calls = []

    choice = router.choose("адм", all_entries(),
                           transport=lambda *a, **k: calls.append(a))
    assert choice.faq_id is None
    assert not calls


def test_choose_respects_the_shared_budget(monkeypatch):
    monkeypatch.setattr(rephrase, "CALLS_PER_CHAT_HOUR", 1)
    first = router.choose("вопрос", all_entries(), chat_id="7",
                          transport=_transport("4"))
    second = router.choose("вопрос", all_entries(), chat_id="7",
                           transport=_transport("4"))
    assert first.faq_id == 4
    assert second.faq_id is None


# --- бот: красная карточка становится ответом -------------------------------

class _Handle:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        return self


QUESTION = "крч там чёт с симкой"


def _refuse(chat_id: int) -> None:
    """Задать вопрос, на который поиск отвечает отказом."""
    screen = bot.screen_for_text(QUESTION, chat_id=chat_id)
    assert screen is not None and screen.text.startswith("🔴")


def test_disabled_router_leaves_the_buttons(monkeypatch):
    monkeypatch.setattr(router, "ENABLED", False)
    _refuse(811)
    handle = _Handle()

    asyncio.run(bot._second_opinion(811, handle))
    assert handle.edits == []


def test_model_choice_rewrites_refusal_with_verbatim_answer(monkeypatch):
    monkeypatch.setattr(router, "ENABLED", True)
    monkeypatch.setattr(router, "choose",
                        lambda *a, **k: router.Choice(4))
    _refuse(812)
    handle = _Handle()

    asyncio.run(bot._second_opinion(812, handle))

    assert len(handle.edits) == 1
    text = handle.edits[0]
    entry = next(item for item in all_entries() if item.id == 4)
    assert text.startswith("🤖")
    assert "подсказала модель" in text
    # Ответ — дословно из базы: модель дала только номер темы.
    assert entry.answer_ru.split(".")[0] in text


def test_model_refusal_leaves_the_buttons(monkeypatch):
    monkeypatch.setattr(router, "ENABLED", True)
    monkeypatch.setattr(router, "choose",
                        lambda *a, **k: router.Choice(None, "не выбрала"))
    _refuse(813)
    handle = _Handle()

    asyncio.run(bot._second_opinion(813, handle))
    assert handle.edits == []


def test_stale_question_is_not_overwritten(monkeypatch):
    """Абонент успел задать новый вопрос — старый отказ не переписываем."""
    monkeypatch.setattr(router, "ENABLED", True)
    monkeypatch.setattr(router, "choose", lambda *a, **k: router.Choice(4))
    _refuse(814)
    noted = bot._SECOND_OPINION[bot._key(814)]
    bot.screen_for_text("Как проверить баланс?", chat_id=814)
    bot._SECOND_OPINION[bot._key(814)] = noted
    handle = _Handle()

    asyncio.run(bot._second_opinion(814, handle))
    assert handle.edits == []
