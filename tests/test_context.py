"""Тесты контекста диалога (пункт 4 ТЗ).

Приёмка: диалог из `data/05_support/dialogs_example.json` проходит
целиком. Остальные тесты следят за тем, чтобы наследование темы не стало
жадным: в проверке жюри 40 обычных вопросов и только 5 диалогов.
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from faq_bot import config
from faq_bot.answer import answer
from faq_bot.context import (
    INHERITED_CONFIDENCE,
    DialogMemory,
    Turn,
    apply_context,
    is_follow_up,
)
from faq_bot.contracts import Decision


class _Clock:
    """Управляемые часы для проверки TTL."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _unsure_ranker(faq_id: int | None = None, confidence: float = 0.12,
                   candidates: tuple[int, ...] = (7, 2, 11)):
    """Поиск, который сам по себе не узнаёт уточняющий вопрос."""
    def rank(text: str, language: str) -> Decision:
        return Decision(faq_id, confidence, list(candidates))
    return rank


# --------------------------------------------------------------------------
# Образец диалога из набора
# --------------------------------------------------------------------------

def test_dialog_example_passes_completely():
    """Обе реплики образца ведут в одну тему в рамках одного chat_id."""
    dialogs = json.loads(config.DIALOGS_PATH.read_text(encoding="utf-8"))

    for number, dialog in enumerate(dialogs):
        chat_id = f"test-{dialog.get('id', number)}"
        for turn in dialog["turns"]:
            result = answer(turn["text"], chat_id=chat_id)
            assert result["faq_id"] == turn["faq_id"], (dialog["id"], turn["text"])
            assert result["lang"] == turn["lang"], (dialog["id"], turn["text"])


def test_follow_up_inherits_topic_when_search_is_unsure(monkeypatch):
    """«А сколько стоит?» после роуминга остаётся в теме 4.

    Поиск в этом тесте нарочно слабый: проверяется именно контекст,
    а не везение ранжирования.
    """
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(4, 0.9, [4, 7, 5]))
    first = module.answer("Как подключить роуминг?", chat_id=1)

    monkeypatch.setattr(module, "rank_question", _unsure_ranker())
    second = module.respond("А сколько стоит?", chat_id=1)

    assert first["faq_id"] == 4
    assert second.faq_id == 4
    assert second.inherited_from_context
    assert second.confidence >= config.CONFIDENCE_THRESHOLD


def test_context_does_not_leak_between_chats(monkeypatch):
    """Контекст одного chat_id не виден другому."""
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(4, 0.9, [4, 7, 5]))
    module.answer("Как подключить роуминг?", chat_id="chat-a")

    monkeypatch.setattr(module, "rank_question", _unsure_ranker())
    stranger = module.respond("А сколько стоит?", chat_id="chat-b")

    assert stranger.faq_id is None
    assert not stranger.inherited_from_context


def test_follow_up_without_context_does_not_crash(monkeypatch):
    """`answer(text)` без chat_id не обязан угадывать тему, но и не падает."""
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question", _unsure_ranker())
    result = module.answer("А сколько стоит?")

    assert result["faq_id"] is None
    assert result["lang"] == "ru"
    assert 0.0 <= result["confidence"] <= 1.0


def test_confident_question_changes_topic(monkeypatch):
    """«А как пополнить счёт?» после роуминга — новая тема, а не уточнение."""
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(4, 0.9, [4, 7, 5]))
    module.answer("Как подключить роуминг?", chat_id=5)

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(11, 0.8, [11, 9, 1]))
    second = module.respond("А как пополнить счёт?", chat_id=5)

    assert second.faq_id == 11
    assert not second.inherited_from_context


def test_reset_context_forgets_topic(monkeypatch):
    """Кнопка «другой вопрос» сбрасывает тему диалога."""
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(4, 0.9, [4, 7, 5]))
    module.answer("Как подключить роуминг?", chat_id=8)
    module.reset_context(8)

    monkeypatch.setattr(module, "rank_question", _unsure_ranker())
    assert module.respond("А сколько стоит?", chat_id=8).faq_id is None


# --------------------------------------------------------------------------
# Признак уточняющего вопроса
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "А сколько стоит?",
    "а почему?",
    "Сколько стоит?",
    "Канча турат?",
    "А когда?",
    "а бесплатно?",
])
def test_recognised_as_follow_up(text):
    """Короткая реплика без собственных ключевых слов — уточнение."""
    assert is_follow_up(text)


@pytest.mark.parametrize("text", [
    "",
    "Как проверить баланс?",
    "А как пополнить счёт?",
    "Роуминг канча турат?",
    "Сколько комиссия за перевод?",
    "Спасибо",
    "Ооба",
    "Бүгүн аба ырайы кандай?",
    "Футболдун эсеби кандай?",
    "Хочу узнать, сколько стоит роуминг в Казахстане на неделю",
])
def test_not_a_follow_up(text):
    """Свои ключевые слова или длина — это самостоятельный вопрос."""
    assert not is_follow_up(text)


def test_out_of_scope_question_does_not_inherit_topic(monkeypatch):
    """Внецелевой вопрос в живом диалоге всё равно получает faq_id = null.

    Жадное наследование темы испортило бы 40 обычных вопросов проверки
    ради 5 диалогов, поэтому «бүгүн аба ырайы кандай?» уточнением
    не считается, хотя и коротко, и со словом «кандай».
    """
    from faq_bot import answer as module

    monkeypatch.setattr(module, "rank_question",
                        lambda text, language: Decision(4, 0.9, [4, 7, 5]))
    module.answer("Роумингди кантип кошом?", chat_id="weather")

    monkeypatch.setattr(module, "rank_question", _unsure_ranker())
    result = module.answer("Бүгүн аба ырайы кандай?", chat_id="weather")

    assert result["faq_id"] is None
    assert result["lang"] == "ky"


# --------------------------------------------------------------------------
# Память диалогов
# --------------------------------------------------------------------------

def test_memory_keeps_last_topic_per_chat():
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)
    memory.remember("b", 11, 0.8)
    memory.remember("a", 2, 0.7)

    assert memory.recent_topic("a") == 2
    assert memory.recent_topic("b") == 11
    assert memory.recent_topic("c") is None
    assert memory.recent_topic(None) is None


def test_memory_ignores_questions_without_topic():
    memory = DialogMemory()
    memory.remember("a", None, 0.1)

    assert memory.turns("a") == ()
    assert memory.recent_topic("a") is None


def test_memory_respects_turn_limit():
    memory = DialogMemory(max_turns=2)
    for faq_id in (1, 2, 3):
        memory.remember("a", faq_id, 0.9)

    assert [turn.faq_id for turn in memory.turns("a")] == [2, 3]


def test_memory_expires_by_ttl():
    clock = _Clock()
    memory = DialogMemory(ttl_seconds=60, clock=clock)
    memory.remember("a", 4, 0.9)

    clock.advance(59)
    assert memory.recent_topic("a") == 4

    clock.advance(2)
    assert memory.recent_topic("a") is None


def test_memory_limits_number_of_chats():
    memory = DialogMemory(max_chats=2)
    for chat_id in ("a", "b", "c"):
        memory.remember(chat_id, 1, 0.9)

    assert len(memory) == 2
    assert memory.recent_topic("a") is None


def test_memory_counts_misses_in_a_row():
    """Отказы подряд считаются: после трёх бот сам предложит оператора."""
    memory = DialogMemory()
    for _ in range(3):
        memory.remember("a", None, 0.1)

    assert memory.misses("a") == 3
    assert memory.misses("b") == 0
    assert memory.misses(None) == 0
    assert memory.recent_topic("a") is None


def test_memory_resets_misses_on_any_answer():
    """Любой найденный ответ обнуляет цепочку отказов."""
    memory = DialogMemory()
    memory.remember("a", None, 0.1)
    memory.remember("a", None, 0.1)
    memory.remember("a", 4, 0.9)

    assert memory.misses("a") == 0
    assert memory.recent_topic("a") == 4


def test_memory_misses_expire_by_ttl():
    """Старая цепочка отказов не висит вечно."""
    clock = _Clock()
    memory = DialogMemory(ttl_seconds=60, clock=clock)
    memory.remember("a", None, 0.1)
    clock.advance(61)

    assert memory.misses("a") == 0
    memory.remember("a", None, 0.1)
    assert memory.misses("a") == 1


def test_memory_forget_misses():
    """Предложили оператора — счётчик отказов сбрасывается."""
    memory = DialogMemory()
    memory.remember("a", None, 0.1)
    memory.forget_misses("a")

    assert memory.misses("a") == 0


@pytest.mark.parametrize("text, expected", [
    ("салам", False),
    ("как дела", False),
    ("рахмат", False),
    ("интернет иштебейт", True),
    ("салам, интернет иштебейт", True),
    ("как проверить баланс", True),
    ("", False),
])
def test_mentions_topic(text, expected):
    """Признак «в реплике есть своё ключевое слово темы» — общий для бота."""
    from faq_bot.context import mentions_topic

    assert mentions_topic(text) is expected


def test_memory_forget_and_clear():
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)
    memory.forget("a")
    assert memory.recent_topic("a") is None

    memory.remember("b", 5, 0.9)
    memory.clear()
    assert len(memory) == 0


# --------------------------------------------------------------------------
# Правило наследования в чистом виде
# --------------------------------------------------------------------------

def test_apply_context_requires_chat_id():
    decision = Decision(None, 0.1, [7])
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)

    assert apply_context(decision, "А сколько стоит?", None, memory) is decision


def test_apply_context_keeps_confident_decision():
    decision = Decision(11, 0.9, [11])
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)

    assert apply_context(decision, "А сколько стоит?", "a", memory) is decision


def test_apply_context_inherits_and_marks_source():
    decision = Decision(None, 0.1, [7, 2, 11])
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)

    updated = apply_context(decision, "А сколько стоит?", "a", memory)

    assert updated.faq_id == 4
    assert updated.candidates[0] == 4
    assert updated.inherited_from_context
    assert updated.confidence == pytest.approx(INHERITED_CONFIDENCE)


def test_apply_context_needs_living_topic():
    clock = _Clock()
    memory = DialogMemory(ttl_seconds=10, clock=clock)
    memory.remember("a", 4, 0.9)
    clock.advance(11)

    decision = Decision(None, 0.1, [7])
    assert apply_context(decision, "А сколько стоит?", "a", memory) is decision


def test_turn_is_immutable():
    """Запомненная реплика — данные, а не изменяемое состояние."""
    turn = Turn(faq_id=4, confidence=0.9, at=0.0)
    with pytest.raises(FrozenInstanceError):
        turn.faq_id = 5          # type: ignore[misc]


# --------------------------------------------------------------------------
# Отмена последней записи: ответ переписывается, а не добавляется
# --------------------------------------------------------------------------

def test_rollback_undoes_the_last_topic():
    """Дописанная реплика пересчитывает ответ, а не добавляет реплику.

    Слой бота отвечает на первую реплику сразу и переписывает свой ответ,
    когда абонент дописывает мысль. Для памяти диалога такая серия
    обязана остаться одной репликой.
    """
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)
    memory.remember("a", 3, 0.6)

    assert memory.rollback("a") is True
    assert memory.recent_topic("a") == 4
    assert len(memory.turns("a")) == 1


def test_rollback_undoes_the_last_miss():
    """Отменяется и продление цепочки отказов: мысль одна, отказ один."""
    memory = DialogMemory()
    memory.remember("a", None)
    memory.remember("a", None)

    assert memory.rollback("a") is True
    assert memory.misses("a") == 1


def test_rollback_of_the_only_miss_clears_the_streak():
    memory = DialogMemory()
    memory.remember("a", None)

    assert memory.rollback("a") is True
    assert memory.misses("a") == 0


def test_rollback_does_not_touch_other_chats():
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)
    memory.remember("b", 11, 0.9)

    memory.rollback("a")

    assert memory.recent_topic("a") is None
    assert memory.recent_topic("b") == 11


def test_rollback_on_empty_memory_is_harmless():
    """Отменять нечего — не исключение, а честное False."""
    memory = DialogMemory()

    assert memory.rollback("a") is False
    assert memory.rollback(None) is False


def test_rollback_happens_once_per_remember():
    """Вторая отмена подряд ничего не отменяет: отменять больше нечего."""
    memory = DialogMemory()
    memory.remember("a", 4, 0.9)
    memory.remember("a", 3, 0.6)

    assert memory.rollback("a") is True
    assert memory.rollback("a") is False
    assert memory.recent_topic("a") == 4


def test_a_thought_in_three_parts_keeps_the_dialog_history():
    """Вопрос из трёх сообщений не вытесняет историю чата.

    Без отмены записи один вопрос, набранный тремя репликами, занял бы
    три места из четырёх, и предыдущая тема разговора потерялась бы.
    """
    memory = DialogMemory(max_turns=config.CONTEXT_TURNS)
    memory.remember("a", 4, 0.9)                 # роуминг — тема разговора
    memory.remember("a", 1, 0.7)                 # первая часть новой мысли
    for _ in range(2):                           # и ещё две дописки
        memory.rollback("a")
        memory.remember("a", 1, 0.7)

    assert [turn.faq_id for turn in memory.turns("a")] == [4, 1]


# --- Статистика при дозревании ответа --------------------------------------

def test_stats_rollback_undoes_the_last_question():
    """Отмена убирает последний учтённый вопрос, а не произвольный.

    Нужна при дозревании: абонент дописывает мысль второй и третьей
    репликой, бот пересчитывает ответ и переписывает своё сообщение.
    Для него это ОДИН вопрос, поэтому лишние записи отменяются.
    """
    from faq_bot.stats import StatsCollector

    collector = StatsCollector()
    collector.record("крч проблема такая", "ru", None, 0.1)
    collector.record("крч проблема такая типа сигнал есть", "ru", None, 0.2)
    collector.record("крч проблема такая типа сигнал есть но инета нет",
                     "ru", 3, 0.9)

    assert collector.snapshot().total == 3

    # Так это и происходит в боте: перед каждым пересчётом отменяется
    # прежняя запись, поэтому в итоге остаётся ровно одна.
    assert collector.rollback() is True
    assert collector.rollback() is True

    snapshot = collector.snapshot()
    assert snapshot.total == 1
    assert snapshot.unanswered == 1


def test_stats_rollback_on_empty_history_is_harmless():
    """Отменять нечего — возвращается False, исключения нет."""
    from faq_bot.stats import StatsCollector

    assert StatsCollector().rollback() is False


def test_one_thought_in_three_messages_counts_as_one_question():
    """Сквозная проверка: дозревание не раздувает /stats.

    Это и был дефект: вопрос из трёх реплик давал три обращения, и
    незаконченная первая часть попадала в примеры неотвеченных.
    """
    from faq_bot import bot
    from faq_bot.answer import STATS

    if hasattr(bot, "reset_runtime_state"):
        bot.reset_runtime_state()
    STATS.reset() if hasattr(STATS, "reset") else None
    before = STATS.snapshot().total

    bot.screen_for_text("крч проблема такая", chat_id=4242)
    bot.screen_for_text("крч проблема такая типа сигнал есть",
                        chat_id=4242, revision=True)
    bot.screen_for_text("крч проблема такая типа сигнал есть но инета нет",
                        chat_id=4242, revision=True)

    assert STATS.snapshot().total - before == 1
