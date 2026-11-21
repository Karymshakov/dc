"""Проверки сторожа фактической согласованности.

Тесты записаны от правила, а не от реализации: проверяется, что ответ
с чужим фактом не проходит, а ответ из своей темы проходит. Внутренние
регулярные выражения и структуры не импортируются.
"""
from __future__ import annotations

import pytest

from faq_bot.consistency import (
    audit_knowledge,
    check,
    extract_facts,
    safe_answer,
)
from faq_bot.contracts import FaqEntry
from faq_bot.knowledge import load_faq


@pytest.fixture(scope="module")
def entries() -> list[FaqEntry]:
    return list(load_faq())


@pytest.fixture(scope="module")
def by_id(entries) -> dict[int, FaqEntry]:
    return {entry.id: entry for entry in entries}


# --- Извлечение проверяемых утверждений ------------------------------------

def test_ussd_code_is_a_checkable_fact():
    facts = extract_facts("Наберите *100# для проверки.")
    assert [str(item) for item in facts] == ["ussd:*100#"]


def test_complex_ussd_with_parameters_is_captured():
    facts = extract_facts("Команда *145*996700000000*100# подтвердит перевод.")
    assert any(item.kind == "ussd" for item in facts)


def test_price_needs_a_unit_to_count_as_a_claim():
    """«100» само по себе — не утверждение о цене, «100 сом» — утверждение."""
    assert not [item for item in extract_facts("нажмите 100") if item.kind == "money"]
    assert [item for item in extract_facts("стоит 100 сом") if item.kind == "money"]


def test_volume_is_captured_in_both_scripts():
    facts = {str(item) for item in extract_facts("1 ГБ за 30 сом, 5 gb дороже")}
    assert "volume:1гб" in facts
    assert "volume:5gb" in facts


def test_repeated_fact_counts_once():
    facts = extract_facts("Наберите *100#. Повторим: *100#.")
    assert len(facts) == 1


def test_text_without_claims_has_no_facts():
    assert extract_facts("Перезагрузите телефон и попробуйте снова.") == ()


# --- Главное правило: проверяется связь, а не наличие ----------------------

def test_real_code_from_another_topic_is_rejected(by_id):
    """Ключевой случай из работы: факт настоящий, привязка чужая.

    Код *111# существует и работает, но принадлежит доп. интернету.
    В ответе про баланс он означает, что абонент наберёт рабочую команду
    и получит не то, за чем пришёл. Проверка «есть ли такой код в базе»
    это пропускает — он в базе есть.
    """
    verdict = check("Наберите *111#. Проверка бесплатная.", by_id[1])

    assert verdict.consistent is False
    assert any(item.kind == "ussd" for item in verdict.violations)


def test_genuine_answer_of_the_topic_passes(by_id):
    for topic in (1, 2, 4, 7, 9):
        entry = by_id[topic]
        assert check(entry.answer_ru, entry).consistent, topic
        assert check(entry.answer_ky, entry).consistent, topic


def test_substituted_price_is_rejected(by_id):
    entry = by_id[7]
    tampered = entry.answer_ru.replace("30", "300")
    if tampered == entry.answer_ru:
        pytest.skip("в теме нет цены для подмены")

    assert check(tampered, entry).consistent is False


def test_answer_without_facts_is_always_consistent(by_id):
    """Тема без кодов и цен не может нарушить связь — нечему нарушаться."""
    verdict = check("Перезагрузите телефон.", by_id[3])

    assert verdict.consistent is True
    assert verdict.checked == ()


def test_kyrgyz_answer_facts_belong_to_the_same_topic(by_id):
    """Русская и кыргызская версии описывают одну услугу, коды совпадают."""
    for entry in (by_id[1], by_id[7], by_id[9]):
        assert check(entry.answer_ky, entry).consistent


# --- Поведение при нарушении ----------------------------------------------

def test_inconsistent_answer_is_replaced_by_the_source(by_id):
    entry = by_id[1]
    text, verdict = safe_answer("Наберите *111#.", entry, "ru")

    assert verdict.consistent is False
    assert text == entry.answer_ru


def test_consistent_answer_is_passed_through(by_id):
    entry = by_id[1]
    text, verdict = safe_answer(entry.answer_ru, entry, "ru")

    assert verdict.consistent is True
    assert text == entry.answer_ru


def test_replacement_respects_the_language(by_id):
    entry = by_id[1]
    text, _ = safe_answer("Наберите *111#.", entry, "ky")

    assert text == entry.answer_ky


def test_verdict_explains_itself(by_id):
    verdict = check("Наберите *111#.", by_id[1])

    assert "не согласован" in verdict.explain().lower()
    assert "*111#" in verdict.explain()


# --- Честный учёт слабого места -------------------------------------------

def test_shared_facts_are_reported_not_hidden(entries):
    """Факт из двух тем проверкой связи не ловится — это должно быть видно.

    Метод проверяет принадлежность факта теме. Если одно и то же значение
    законно принадлежит двум темам, подмена между ними пройдёт. Такие
    места перечисляются явно, чтобы ограничение было известно заранее,
    а не обнаружилось у жюри.
    """
    shared = audit_knowledge(entries)

    assert isinstance(shared, dict)
    for fact, owners in shared.items():
        assert len(owners) > 1, fact


def test_every_topic_fact_belongs_to_its_own_topic(entries):
    """Самосогласованность базы: факты записи проходят проверку этой записи."""
    for entry in entries:
        assert check(entry.searchable(), entry).consistent, entry.id


# --- Стоимость -------------------------------------------------------------

def test_check_makes_no_model_calls_and_is_fast(by_id):
    """Контроль дешевле генерации — иначе он не доживает до продакшена.

    В исходной работе правила отрабатывали за 52 мс против 885 мс
    у решения с обращением к модели. Здесь объём меньше на порядки,
    поэтому запас на медленную машину взят щедрый.
    """
    import time

    entry = by_id[7]
    started = time.perf_counter()
    for _ in range(1000):
        check(entry.answer_ru, entry)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"1000 проверок заняли {elapsed:.2f} с"
