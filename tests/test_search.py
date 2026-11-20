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
from faq_bot.search_fuzzy import (
    BACKEND,
    STEM_LENGTH,
    FuzzySearch,
    shares_stem,
    token_similarity,
)
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


def test_подтверждает_тему_только_различающее_слово():
    """Общий корень не подтверждение: «купить» есть у темы 7 и больше нигде.

    Это второй сигнал отсева, адресный: не «знает ли область эти слова»,
    а «есть ли у ЭТОЙ темы слово из вопроса». Живая ошибка, из-за которой
    он появился: «как купить симку» уходило в тему 7 «дополнительный
    интернет», потому что «купить» стоит в её синонимах, а «симку»
    у неё не находило ничего.
    """
    fuzzy = FuzzySearch(ENTRIES)
    supported = fuzzy.supported_topics("как купить симку")
    assert 6 in supported, "«симку» — слово темы про SIM"
    assert 7 not in supported, "у темы 7 только общий корень «купить»"
    # Вопрос из одних общих корней не подтверждает вообще ничего.
    assert fuzzy.supported_topics("как это работает") == set()
    assert fuzzy.supported_topics("") == set()
    # Опечатка подтверждение не теряет: сравнение слов то же нечёткое.
    assert 1 in fuzzy.supported_topics("как проверть балнс")
    # Дословный вопрос базы обязан подтверждать свою тему на двух языках.
    for entry in ENTRIES:
        for language in config.LANGUAGES:
            assert entry.id in fuzzy.supported_topics(entry.question(language)), (
                entry.id, language)


def test_общая_основа_это_не_ещё_один_порог_похожести():
    """Своё слово темы отличается от нечёткого соседа основой, а не числом.

    Четыре замера, на которых правило согласия ролей и строилось: два
    ложных совпадения проходят по PAIR_THRESHOLD, две нужные словоформы
    стоят рядом с ними по числу. По числу они не разделяются, по основе
    разделяются начисто.
    """
    # Нужные словоформы: тот же глагол, другой аффикс.
    assert shares_stem("озгортсом", "озгортуу")
    assert shares_stem("алмаштыргым", "алмаштыруу")
    assert shares_stem("тарифимди", "тарифти")
    assert shares_stem("номеримди", "номерди")
    assert shares_stem("симканы", "симка")
    assert shares_stem("тариф", "тариф")
    # Ложные совпадения: букв похоже, основы нет.
    assert not shares_stem("досума", "доступа")
    assert not shares_stem("салсам", "алам")
    assert not shares_stem("билет", "билем")
    for left, right in (("досума", "доступа"), ("салсам", "алам")):
        assert token_similarity(left, right) >= 0.75, (
            f"{left}/{right} обязаны быть ловушкой для порога похожести")
    # Короткое слово родственников не имеет: только дословное совпадение.
    assert not shares_stem("гб", "гбайт")
    assert len("гб") < STEM_LENGTH


def test_действие_подтверждает_тему_только_вместе_со_своим_объектом():
    """Третий гейт: действие одной темы плюс объект другой.

    Живая ошибка: «сменить номер» уходило в тему 5 «Как сменить тариф?»
    с уверенностью 0.63. Глагол принадлежит теме 5 законно и встречается
    в базе только у неё, поэтому общим корнем он быть не может. Ложным
    его делает объект: смены номера оператор не предлагает вовсе, и ни
    одна тема не содержит обоих слов. Подтверждать тему тогда не может
    ничто — вопрос уходит в отказ с кнопками.
    """
    fuzzy = FuzzySearch(ENTRIES)
    # Операции нет в базе: подтверждения нет ни у кого.
    for text in ("сменить номер", "как мне сменить номер?", "поменять номер",
                 "восстановить тариф", "заблокировать тариф",
                 "пополнить тариф", "номерди алмаштыруу"):
        assert fuzzy.supported_topics(text) == set(), text
    # Тот же глагол со своим объектом подтверждает свою тему.
    assert 5 in fuzzy.supported_topics("хочу сменить тариф")
    assert 5 in fuzzy.supported_topics("Тарифти алмаштыруу")
    assert 6 in fuzzy.supported_topics("восстановить симку")
    assert 13 in fuzzy.supported_topics("заблокировать номер")
    assert 11 in fuzzy.supported_topics("пополнить счет")
    assert 9 in fuzzy.supported_topics("перевести на другой номер")
    # Одна роль в запросе — сверять не с чем, правило молчит.
    assert 9 in fuzzy.supported_topics("перевести деньги")
    assert 2 in fuzzy.supported_topics("остаток минут")


def test_правило_ролей_опирается_на_слова_темы_а_не_на_опечатки():
    """Правило разрушительное, поэтому нечёткого совпадения ему мало.

    Оба вопроса законны, и в обоих есть слово, которое по похожести
    попадает в чужую тему: «досума» (моему другу) похоже на «доступа»
    темы 10, «салсам» (если положу) — на «алам» темы 15. Пока правило
    верило похожести, оно объявляло оба запроса противоречивыми и
    отнимало подтверждение у верной темы — у 9 и 11 соответственно.
    """
    fuzzy = FuzzySearch(ENTRIES)
    assert 9 in fuzzy.supported_topics("Досума акча которсом болобу?")
    assert 11 in fuzzy.supported_topics("Телефонго акча кайдан салсам болот?")


def test_словарное_покрытие_отличает_чужую_лексику():
    fuzzy = FuzzySearch(ENTRIES)
    assert fuzzy.vocabulary_coverage("Прогноз погоды на завтра") == 0.0
    assert fuzzy.vocabulary_coverage("") == 0.0
    assert fuzzy.vocabulary_coverage("Как проверить баланс?") > 0.9
