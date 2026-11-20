"""Устойчивость под нагрузкой: длина входа и рост состояния.

Эти проверки появились не из осторожности, а после двух измерений.

**Первое.** Замер нагрузки завис. Не упал — завис, и это оказалось
хуже: на ОДНОМ длинном слове без пробелов стоимость поиска по
символьным n-граммам растёт квадратично. 1 000 знаков — 6 мс, 10 000 —
437 мс, 30 000 — 3,75 секунды. Миллион знаков занял бы больше часа.
Бот свой ввод ограничивает, но `answer()` вызывают напрямую.

**Второе.** Состояние разговора хранилось в обычных словарях по
`chat_id`: язык, счётчики ограничителя, окно дозревания, последняя
реплика для оператора. Запись появлялась на первом сообщении и не
исчезала никогда. Для демонстрации это незаметно, для оператора связи —
утечка памяти и дешёвая атака: писать с множества аккаунтов, не нарушая
ни одного ограничения на частоту, потому что сам ограничитель хранится
в таком же словаре.
"""
from __future__ import annotations

import time

import pytest

from faq_bot import bot, config, handoff
from faq_bot.answer import answer
from faq_bot.bounded import DEFAULT_LIMIT, BoundedDict, BoundedSet

# Потолок времени намеренно щедрый: тест охраняет порядок величины,
# а не скорость машины. Настоящий разрыв здесь — часы против миллисекунд.
SLOW_MS = 200.0


def _ms(call) -> float:
    start = time.perf_counter()
    call()
    return (time.perf_counter() - start) * 1000


@pytest.mark.parametrize("size", [10_000, 100_000, 1_000_000])
def test_one_enormous_word_does_not_hang(size):
    """Одно слово на миллион знаков обязано отвечать мгновенно."""
    text = "а" * size

    assert _ms(lambda: answer(text)) < SLOW_MS


def test_enormous_input_of_every_shape(monkeypatch):
    """Форма входа значит не меньше размера: пробелы, цифры, латиница."""
    for text in ("а" * 400_000, "a" * 400_000, "1" * 400_000,
                 "🟢" * 200_000, "баланс " * 60_000):
        assert _ms(lambda t=text: answer(t)) < SLOW_MS


def test_truncation_keeps_the_answer_findable():
    """Обрезка не отказ: вопрос в начале длинного мусора находится.

    Важно именно это: защита не должна превращать длинный вход в
    бесполезный отказ, если вопрос в нём всё-таки есть.
    """
    padded = "Как проверить баланс? " + "ещё текста " * 5_000

    result = answer(padded)

    assert result["faq_id"] == 1


def test_contract_holds_on_the_longest_input():
    """Поля ответа те же: обрезка не меняет формат, по которому считают."""
    result = answer("б" * (config.MAX_INPUT_CHARS * 100))

    assert set(result) == {"lang", "faq_id", "confidence", "candidates"}
    assert result["faq_id"] is None
    assert 0.0 <= result["confidence"] <= 1.0


# --------------------------------------------------------------------------
# Рост состояния
# --------------------------------------------------------------------------


def test_bounded_dict_drops_the_oldest():
    store: BoundedDict[int, int] = BoundedDict(3)

    for key in range(5):
        store[key] = key

    assert list(store) == [2, 3, 4]
    assert len(store) == 3


def test_bounded_dict_rewrite_is_not_an_insert():
    """Перезапись существующего ключа никого не вытесняет."""
    store: BoundedDict[str, int] = BoundedDict(2)
    store["a"] = 1
    store["b"] = 2

    store["a"] = 9

    assert dict(store) == {"a": 9, "b": 2}


def test_bounded_set_drops_the_oldest():
    seen: BoundedSet[int] = BoundedSet(2)

    for key in range(4):
        seen.add(key)

    assert sorted(seen) == [2, 3]


def test_zero_limit_is_refused():
    """Потолок меньше одной записи — ошибка настройки, не поведение."""
    with pytest.raises(ValueError):
        BoundedDict(0)
    with pytest.raises(ValueError):
        BoundedSet(0)


def test_state_of_many_chats_stays_bounded():
    """Поток разных собеседников не растит состояние бесконечно.

    Проходим в полтора потолка и проверяем каждое хранилище: язык,
    счётчики ограничителя, окно дозревания, последняя реплика для
    оператора. Без потолка любое из них росло бы линейно по числу чатов.
    """
    chats = DEFAULT_LIMIT + DEFAULT_LIMIT // 2

    for chat_id in range(chats):
        bot.set_chat_language(chat_id, "ru")
        bot.throttle().check(chat_id)
        handoff.DESK.note_question(chat_id, "скока денег на счету")

    assert len(bot._chat_language) <= DEFAULT_LIMIT
    assert len(bot.throttle()._sent) <= DEFAULT_LIMIT
    assert len(handoff.DESK._turns) <= DEFAULT_LIMIT
