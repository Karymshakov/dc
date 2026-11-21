"""Объяснение решения (`/why`): оно обязано совпадать с настоящим ответом.

Главная опасность такого экрана — рассказать одно, а ответить другое.
Поэтому тесты сверяют объяснение с `answer()`, а не с ожидаемым текстом.
"""
from __future__ import annotations

import pytest

from faq_bot import bot, config, explain
from faq_bot.answer import answer

QUESTIONS = [
    "Скока денег на счету?",
    "Balansymdy kantip korom",
    "Роуминг канча турат?",
    "крч там чёт с симкой",
    "Прогноз погоды на завтра",
]


@pytest.mark.parametrize("question", QUESTIONS)
def test_explanation_agrees_with_answer(question):
    """Тема, уверенность и язык в объяснении — те же, что отдаёт answer()."""
    explained = explain.explain(question)
    contract = answer(question)

    assert explained.verdict.language == contract["lang"]
    assert explained.decision.faq_id == contract["faq_id"]
    assert round(explained.decision.confidence, 3) == contract["confidence"]


def test_latin_kyrgyz_shows_language_evidence():
    """Кыргызская латиница: язык ky и названы улики, по которым он выбран."""
    explained = explain.explain("Balansymdy kantip korom")

    assert explained.verdict.language == "ky"
    assert explained.verdict.signals
    assert "кыргызский" in explain.render(explained)


def test_both_search_methods_vote():
    """Пункт ТЗ про два метода виден в каждом объяснении."""
    explained = explain.explain("Как проверить баланс?")

    assert [vote.method for vote in explained.votes] == ["tfidf", "fuzzy"]
    assert {vote.faq_id for vote in explained.votes} == {1}
    assert explained.methods_agree


def test_level_follows_config_thresholds():
    """Светофор объяснения считается по тем же порогам, что и у ответа."""
    assert explain.explain("Как проверить баланс?").level == "green"
    rejected = explain.explain("Прогноз погоды на завтра")
    assert rejected.level == "red"
    assert rejected.decision.confidence < config.CONFIDENCE_THRESHOLD
    assert "не отвечаем" in explain.render(rejected)


@pytest.mark.parametrize("text", ["", "   ", None, 42, "?" * 5000])
def test_explain_never_raises(text):
    """Пустой, нестроковый и огромный вход не роняют объяснение."""
    assert isinstance(explain.render(explain.explain(text)), str)


def test_why_screen_without_question_gives_a_hint():
    """/why в чате, где ещё ничего не спрашивали, — подсказка, а не ошибка."""
    bot.reset_runtime_state()
    assert "/why" in bot.why_screen(chat_id=901).text


def test_why_screen_explains_last_question_of_this_chat_only():
    """Разбирается последний вопрос своего чата; чужой чат его не видит."""
    bot.reset_runtime_state()
    bot.screen_for_text("Роуминг канча турат?", chat_id=902)

    own = bot.why_screen(chat_id=902).text
    assert "Роуминг канча турат?" in own
    assert "Два метода поиска" in own
    assert "Роуминг" not in bot.why_screen(chat_id=903).text


def test_why_screen_with_text_explains_that_text():
    """/why <текст> разбирает переданный текст и экранирует разметку."""
    bot.reset_runtime_state()
    screen = bot.why_screen(chat_id=904, text="<b>баланс</b>")

    assert "&lt;b&gt;" in screen.text
    assert screen.parse_mode == "HTML"
