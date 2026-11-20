"""Проверки решения: порог, топ-3, отсев внецелевых, калибровка.

Главный инвариант, на который опирается бот и жюри: тема выдана тогда и
только тогда, когда уверенность не ниже порога. Если он сломается,
кнопки «похожие вопросы» начнут появляться вместе с ответом.
"""
from __future__ import annotations

import pytest

from faq_bot import config
from faq_bot.contracts import Decision
from faq_bot.knowledge import default_faq
from faq_bot.ranking import (
    DEFAULT_METHOD,
    METHODS,
    RAW_CONFIDENT,
    RAW_THRESHOLD,
    Ranker,
    calibrate,
    compare_methods,
    decide,
    load_cases,
    rank,
)

ENTRIES = default_faq()
RANKER = Ranker(ENTRIES, method=DEFAULT_METHOD)

OUT_OF_SCOPE = ("Прогноз погоды на завтра", "Бүгүн аба ырайы кандай?",
                "Кредит на машину оформите", "Кто выиграл матч вчера?",
                "Расскажи анекдот", "", "   ")


# --- калибровка ------------------------------------------------------------


def test_калибровка_монотонна_и_в_отрезке():
    previous = -1.0
    for step in range(101):
        value = calibrate(step / 100)
        assert 0.0 <= value <= 1.0
        assert value >= previous, step
        previous = value


def test_калибровка_привязана_к_порогам_из_config():
    """Сырой порог, выбранный по tune, обязан совпасть с порогом config."""
    assert calibrate(RAW_THRESHOLD) == pytest.approx(config.CONFIDENCE_THRESHOLD)
    assert calibrate(RAW_CONFIDENT) == pytest.approx(config.CONFIDENCE_CONFIDENT)
    assert calibrate(0.0) == 0.0
    assert calibrate(1.0) == 1.0
    # Выходы за отрезок не ломают шкалу.
    assert calibrate(-5.0) == 0.0 and calibrate(5.0) == 1.0


# --- решение ---------------------------------------------------------------


def test_уверенность_всегда_в_отрезке_ноль_один():
    probes = [*OUT_OF_SCOPE, "Как проверить баланс?", "???", "а" * 500,
              "Balansymdy kantip korom", "как", "кантип"]
    for text in probes:
        decision = decide(text)
        assert isinstance(decision, Decision)
        assert 0.0 <= decision.confidence <= 1.0, text


def test_тема_выдана_тогда_и_только_тогда_когда_уверенность_выше_порога():
    probes = [*OUT_OF_SCOPE, *[entry.question_ru for entry in ENTRIES],
              "Инет не грузит", "Скока денег на счету?", "Оператор керек",
              "Сколько стоит доллар сегодня?"]
    for text in probes:
        decision = RANKER.decide(text)
        above = decision.confidence >= config.CONFIDENCE_THRESHOLD
        assert (decision.faq_id is not None) == above, (text, decision)


def test_кандидатов_не_больше_трёх_и_первый_совпадает_с_темой():
    for entry in ENTRIES:
        decision = decide(entry.question_ru)
        assert len(decision.candidates) <= config.TOP_K
        assert decision.candidates[0] == decision.faq_id == entry.id


def test_кандидаты_есть_и_при_отказе():
    """Ниже порога пользователю показывают три похожих вопроса кнопками.

    Поэтому при faq_id = None список кандидатов должен оставаться
    осмысленным, кроме случая, когда искать вообще нечего.
    """
    decision = decide("Прогноз погоды на завтра")
    assert decision.faq_id is None
    assert 0 < len(decision.candidates) <= config.TOP_K


def test_пустой_вопрос_даёт_отказ_без_кандидатов():
    for text in ("", "   ", "\n", "???"):
        decision = decide(text)
        assert decision.faq_id is None
        assert decision.confidence == 0.0
        assert decision.candidates == []


def test_внецелевой_вопрос_даёт_null():
    """12.5% вопросов набора обязаны получить faq_id = null."""
    for text in OUT_OF_SCOPE:
        decision = decide(text)
        assert decision.faq_id is None, (text, decision)
        assert decision.confidence < config.CONFIDENCE_THRESHOLD


def test_дословный_вопрос_базы_даёт_свою_тему_уверенно():
    for entry in ENTRIES:
        for language in config.LANGUAGES:
            decision = decide(entry.question(language))
            assert decision.faq_id == entry.id, (entry.id, language)
            assert decision.confidence >= config.CONFIDENCE_CONFIDENT


def test_опечатка_в_одном_символе_не_меняет_тему():
    for text, faq_id in (("Как проверть баланс?", 1), ("Как сменить тарив?", 5),
                         ("Как пополнить счот?", 11)):
        assert decide(text).faq_id == faq_id, text


def test_кыргызская_и_русская_формулировки_дают_одну_тему():
    for entry in ENTRIES:
        assert decide(entry.question_ru).faq_id == decide(entry.question_ky).faq_id


def test_разговорные_формулировки_без_общих_слов_с_базой():
    """Главная трудность задачи: синонимы вместо совпадения слов.

    Ни один из этих вопросов не делит значимых слов с вопросом базы.
    Деньги при этом ведут в три разные темы, и они обязаны различаться.
    """
    probes = ((" Скока денег на счету?", 1), ("Инет не грузит, что делать", 3),
              ("Как закинуть денег на телефон?", 11),
              ("Перекинуть деньги другу как?", 9),
              ("Позовите живого оператора", 14))
    for text, faq_id in probes:
        decision = decide(text)
        assert decision.faq_id == faq_id, (text, decision)


def test_один_общий_корень_не_считается_ответом():
    """Совпадение по общему корню без подтверждения — повод для кнопок.

    Живая ошибка: вопрос про самого бота «а как ты работаешь» делил с темой
    «Почему не работает интернет?» корень «работа» и получал её как ответ
    с уверенностью выше порога. Правило: если тему не подтверждает ни одно
    различающее слово запроса, тема не выдаётся. В кандидатах она остаётся —
    пользователь увидит три похожих вопроса кнопками.
    """
    for text in ("а как ты работаешь", "как это работает", "а что ты умеешь",
                 "сколько стоит?", "хочу подключить", "можно получить?"):
        decision = RANKER.decide(text)
        assert decision.faq_id is None, (text, decision)
        assert decision.confidence < config.CONFIDENCE_THRESHOLD, (text, decision)


def test_подтверждённая_тема_обгоняет_тему_на_общем_корне():
    """«Купить симку» — это про SIM, а не про покупку гигабайтов.

    Штраф применяется до сортировки, поэтому неподтверждённая тема не
    остаётся первой кнопкой только за счёт более высокой похожести.
    """
    decision = RANKER.decide("как купить симку")
    assert decision.candidates, decision
    assert decision.candidates[0] == 6, decision
    assert decision.faq_id in (None, 6), decision


def test_омонимичный_внецелевой_вопрос_даёт_null():
    """Счёт в игре — не счёт абонента, и порогом это не лечится.

    Живой контрпример: «Футболдун эсеби кандай?» получал тему «баланс»
    с уверенностью 0.47, то есть выше, чем у настоящих вопросов про
    баланс (0.41). Поэтому вопрос решается знанием, а не числом, и
    проверка стоит здесь: отказ обязан быть при любом разумном пороге.
    """
    for text in ("Футболдун эсеби кандай?", "Какой счёт в матче?",
                 "Оюндун эсеби канча?", "матчтын эсеби кандай",
                 "какой счет у игры"):
        decision = RANKER.decide(text)
        assert decision.faq_id is None, (text, decision)
        assert decision.confidence < config.CONFIDENCE_THRESHOLD, (text, decision)


def test_законный_вопрос_про_счёт_абонента_не_пострадал():
    """Разведение омонимов не имеет права стоить темы 1 и 11."""
    for text, faq_id in (("Эсебимде канча сом калды?", 1), ("счет на нуле", 1),
                         ("состояние счета", 1), ("Эсепке акча салуу", 11),
                         ("Эсебимди кайдан толуктасам болот?", 11)):
        decision = RANKER.decide(text)
        assert decision.faq_id == faq_id, (text, decision)


def test_уверенность_осмысленна_на_разговорных_вопросах():
    """Явный вопрос заметно выше порога, внецелевой заметно ниже.

    Численного совпадения с эталоном ТЗ не требует, но шкала должна
    различать «уверен» и «не уверен», иначе цвет ответа бессмыслен.
    """
    for text in ("Скока денег на счету?", "баланс покажи", "Оператор керек",
                 "Инет не грузит, что делать", "Balansymdy kantip korom"):
        decision = RANKER.decide(text)
        assert decision.confidence >= config.CONFIDENCE_CONFIDENT, (text, decision)
    for text in OUT_OF_SCOPE:
        decision = RANKER.decide(text)
        assert decision.confidence < config.CONFIDENCE_THRESHOLD, (text, decision)


def test_кыргызская_латиница_доходит_до_темы():
    assert decide("Balansymdy kantip korom").faq_id == 1
    assert decide("Internet ishtebei jatat").faq_id == 3


# --- сравнение методов -----------------------------------------------------


def test_все_режимы_дают_корректное_решение():
    for method in METHODS:
        ranker = Ranker(ENTRIES, method=method)
        decision = ranker.decide("Как проверить баланс?")
        assert decision.faq_id == 1, method
        assert 0.0 <= decision.confidence <= 1.0


def test_неизвестный_метод_отвергается():
    with pytest.raises(ValueError):
        Ranker(ENTRIES, method="эмбеддинги-которых-нет")


def test_rank_возвращает_калиброванные_оценки_по_убыванию():
    matches = rank("Скока денег на счету?")
    assert 0 < len(matches) <= config.TOP_K
    scores = [match.score for match in matches]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert matches[0].score == pytest.approx(decide("Скока денег на счету?").confidence)


def test_точность_на_открытом_наборе_не_деградирует():
    """Защита от регрессии: цифры из отчёта должны воспроизводиться.

    Порог теста ниже достигнутого (100% на tune) намеренно: тест ловит
    поломку, а не фиксирует подгонку под конкретные 25 вопросов.
    """
    if not config.TUNE_PATH.exists():
        pytest.skip("tune.csv ещё не создан: python tools/split.py")
    cases = load_cases(config.TUNE_PATH)
    reports = {report.method: report for report in compare_methods(cases)}
    best = reports[DEFAULT_METHOD]
    assert best.in_scope_top1 / best.in_scope >= 0.85
    assert best.in_scope_top3 / best.in_scope >= 0.90
    assert best.out_of_scope_ok == best.out_of_scope
    # Выбранный метод обязан быть не хуже альтернативного: иначе в боте
    # работает проигравший, а ТЗ требует лучшего.
    other = reports["fuzzy" if DEFAULT_METHOD == "tfidf" else "tfidf"]
    assert best.top1 >= other.top1
