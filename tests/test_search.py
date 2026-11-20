"""Проверки двух методов поиска.

Оба метода проверяются одними и теми же тестами: это и есть смысл
протокола SearchMethod. Если тест написан про один метод, значит он
проверяет реализацию, а не правило.
"""
from __future__ import annotations

import difflib
import importlib
import sys
import types

import pytest

from faq_bot import config
from faq_bot.contracts import Match, SearchMethod
from faq_bot.knowledge import default_faq
from faq_bot.search_fuzzy import BACKEND, FuzzySearch, token_similarity
from faq_bot.search_tfidf import TfidfSearch, char_ngrams

ENTRIES = default_faq()
METHODS = (TfidfSearch(ENTRIES), FuzzySearch(ENTRIES))
IDS = [method.name for method in METHODS]

#: Вопросы, которых нет в базе: погода, кредит, спорт, пустая строка.
OUT_OF_SCOPE = ("Прогноз погоды на завтра", "Бүгүн аба ырайы кандай?",
                "Кредит на машину оформите", "Кто выиграл матч вчера?", "")

#: Мусор и вырожденные входы: метод обязан молчать, а не падать.
JUNK = ("", "   ", "\n\t", "???", "!!!...", "12345", "как", "кантип")


@pytest.fixture(params=METHODS, ids=IDS)
def method(request) -> SearchMethod:
    return request.param


def test_метод_соответствует_протоколу(method):
    assert isinstance(method, SearchMethod)
    assert isinstance(method.name, str) and method.name


def test_не_больше_трёх_кандидатов_и_по_убыванию(method):
    for entry in ENTRIES:
        matches = method.search(entry.question_ru, limit=config.TOP_K)
        assert len(matches) <= config.TOP_K
        scores = [match.score for match in matches]
        assert scores == sorted(scores, reverse=True)
        assert len({match.faq_id for match in matches}) == len(matches)


def test_оценка_всегда_в_отрезке_ноль_один(method):
    probes = [*JUNK, *OUT_OF_SCOPE, "Как проверить баланс?",
              "Balansymdy kantip korom", "а" * 500]
    for text in probes:
        for match in method.search(text, limit=10):
            assert isinstance(match, Match)
            assert 0.0 <= match.score <= 1.0, (text, match)


def test_мусорный_запрос_не_ломает_метод(method):
    for text in JUNK:
        assert method.search(text) == []


def test_дословный_вопрос_базы_находит_свою_тему(method):
    """Самая слабая возможная проверка: вопрос слово в слово из базы."""
    for entry in ENTRIES:
        for language in config.LANGUAGES:
            matches = method.search(entry.question(language), limit=1)
            assert matches, (entry.id, language)
            assert matches[0].faq_id == entry.id, (entry.id, language, matches)
            assert matches[0].score >= 0.75, (entry.id, language, matches)


def test_опечатка_в_одном_символе_не_меняет_тему(method):
    """Устойчивость к опечатке — причина, по которой выбраны эти методы."""
    typos = (("Как проверть баланс?", 1), ("Как провирить баланс?", 1),
             ("Как сменить тарив?", 5), ("Как настроить APM?", 10),
             ("Как пополнить счот?", 11), ("Как узнать свой номир?", 12))
    for text, faq_id in typos:
        matches = method.search(text, limit=1)
        assert matches and matches[0].faq_id == faq_id, (text, matches)


def test_кыргызская_и_русская_формулировки_дают_одну_тему(method):
    for entry in ENTRIES:
        russian = method.search(entry.question_ru, limit=1)
        kyrgyz = method.search(entry.question_ky, limit=1)
        assert russian and kyrgyz, entry.id
        assert russian[0].faq_id == kyrgyz[0].faq_id == entry.id


def test_кыргызская_латиница_находит_тему(method):
    """«balansymdy kantip korom» — это тема 1, а не отказ."""
    for text, faq_id in (("Balansymdy kantip korom", 1),
                         ("Internet ishtebei jatat", 3),
                         ("Tarifti almashtyrgym kelet", 5)):
        matches = method.search(text, limit=config.TOP_K)
        assert matches, text
        assert faq_id in [match.faq_id for match in matches], (text, matches)


def test_внецелевой_вопрос_не_получает_высокой_оценки(method):
    """Отсев внецелевых делает ranking, но оценка должна быть низкой.

    Порог выбирается по tune и равен 0.30 в сыром масштабе; внецелевой
    вопрос обязан остаться заметно ниже него.
    """
    for text in OUT_OF_SCOPE:
        matches = method.search(text, limit=1)
        if matches:
            assert matches[0].score < 0.30, (text, matches)


def test_limit_соблюдается(method):
    assert method.search("Как проверить баланс?", limit=0) == []
    assert len(method.search("Как проверить баланс?", limit=1)) == 1
    assert len(method.search("Как проверить баланс?", limit=5)) <= 5


# --- частности реализации, которые всё же стоит закрепить ------------------


def test_символьные_нграммы_строятся_с_краями():
    grams = char_ngrams("ab", low=3, high=3)
    assert grams[" ab"] == 1 and grams["ab "] == 1


def test_нечёткий_метод_работает_без_rapidfuzz():
    """rapidfuzz не обязательная зависимость: без него берётся difflib."""
    assert BACKEND in ("rapidfuzz", "difflib")
    assert token_similarity("баланс", "баланс") == 1.0
    assert token_similarity("", "баланс") == 0.0
    assert token_similarity("балнс", "баланс") > 0.75
    # Агглютинация: общая основа важнее длины хвоста.
    assert token_similarity("тарифти", "тарифимди") > 0.75


def test_ветка_rapidfuzz_работает_так_же(monkeypatch):
    """Путь с rapidfuzz обязан работать, даже если локально пакета нет.

    У жюри rapidfuzz может оказаться установленным, и тогда включится
    ветка, которую локально никто не проверял. Подставляем поддельный
    пакет с тем же интерфейсом и прогоняем те же утверждения.
    """
    fake = types.ModuleType("rapidfuzz")
    fake.fuzz = types.SimpleNamespace(
        ratio=lambda left, right: difflib.SequenceMatcher(
            None, left, right).ratio() * 100.0)
    monkeypatch.setitem(sys.modules, "rapidfuzz", fake)

    module = importlib.import_module("faq_bot.search_fuzzy")
    try:
        module = importlib.reload(module)
        assert module.BACKEND == "rapidfuzz"
        assert module.token_similarity("балнс", "баланс") > 0.75
        index = module.FuzzySearch(ENTRIES)
        assert index.backend == "rapidfuzz"
        for entry in ENTRIES:
            matches = index.search(entry.question_ru, limit=config.TOP_K)
            assert matches and matches[0].faq_id == entry.id
            assert all(0.0 <= match.score <= 1.0 for match in matches)
        assert index.search("") == []
    finally:
        monkeypatch.delitem(sys.modules, "rapidfuzz")
        importlib.reload(module)


def test_словарное_покрытие_отличает_чужую_лексику():
    fuzzy = FuzzySearch(ENTRIES)
    assert fuzzy.vocabulary_coverage("Прогноз погоды на завтра") == 0.0
    assert fuzzy.vocabulary_coverage("") == 0.0
    assert fuzzy.vocabulary_coverage("Как проверить баланс?") > 0.9
