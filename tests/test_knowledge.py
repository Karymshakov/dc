"""Проверки базы знаний и слоя синонимов.

Тесты проверяют правила, а не реализацию: состав базы, нормализацию
и дисциплину наполнения ключевых слов. Последнее важнее всего: слой
синонимов — главный рычаг точности, и он же главный соблазн подогнать
решение под открытый набор.
"""
from __future__ import annotations

import csv

import pytest

from faq_bot import config
from faq_bot.knowledge import (
    KEYWORDS,
    build_fields,
    content_terms,
    content_text,
    default_faq,
    fold,
    load_faq,
    missing_keywords,
    transliterate,
)


@pytest.fixture(scope="module")
def entries():
    return default_faq()


def test_база_содержит_пятнадцать_стабильных_тем(entries):
    assert len(entries) == config.FAQ_SIZE
    assert [entry.id for entry in entries] == list(range(1, config.FAQ_SIZE + 1))


def test_у_каждой_темы_есть_слой_синонимов(entries):
    assert missing_keywords(entries) == []
    for entry in entries:
        assert len(entry.keywords) >= 5, f"тема {entry.id} почти без синонимов"


def test_синонимы_есть_на_обоих_языках(entries):
    """В каждой теме должны быть и русские, и кыргызские формы.

    Иначе тема находится только на одном языке, а ТЗ требует двух.
    """
    kyrgyz_letters = set("өүңһ")
    for entry in entries:
        kyrgyz = [word for word in entry.keywords
                  if set(word) & kyrgyz_letters
                  or any(part in word for part in ("кантип", "канча", "акча",
                                                   "номерим", "кыз", "жок",
                                                   "сим карта", "калдыгы",
                                                   "жазылуу", "эсеп", "байланыш",
                                                   "чыгым", "тариф", "детализация",
                                                   "интернет", "оператор", "apn",
                                                   "гб", "роуминг"))]
        assert kyrgyz, f"тема {entry.id} без кыргызских синонимов"


def test_ключевые_слова_не_повторяются_между_темами():
    """Одна формулировка не может принадлежать двум темам.

    Повтор означает, что две темы спорят за один и тот же запрос, и выбор
    между ними становится случайным.
    """
    owners: dict[str, list[int]] = {}
    for faq_id, words in KEYWORDS.items():
        for word in words:
            owners.setdefault(content_text(word, expand_latin=False), []).append(faq_id)
    collisions = {text: ids for text, ids in owners.items()
                  if len(set(ids)) > 1}
    assert not collisions, f"одинаковые ключевые слова у разных тем: {collisions}"


def test_ключевые_слова_не_копируют_вопросы_открытого_набора():
    """Защита от подгонки под tune.

    Синонимы — это знание предметной области: «балик», «акча салуу»,
    «точка доступа». Копия размеченной формулировки даёт красивую цифру
    на tune и ничего не даёт на скрытом наборе, поэтому запрещена.
    """
    if not config.TUNE_PATH.exists():
        pytest.skip("tune.csv ещё не создан: python tools/split.py")
    with config.TUNE_PATH.open(encoding="utf-8-sig", newline="") as handle:
        questions = {content_text(row.get("text") or "", expand_latin=False)
                     for row in csv.DictReader(handle)}
    questions.discard("")
    keywords = {content_text(word, expand_latin=False)
                for words in KEYWORDS.values() for word in words}
    assert not (questions & keywords), (
        "ключевое слово дословно повторяет вопрос из tune: "
        f"{sorted(questions & keywords)}")


def test_нормализация_выбрасывает_мусор_и_хранит_коды():
    assert fold("") == ""
    assert fold("   \n\t  ") == ""
    assert fold("!!! ??? ...") == ""
    assert fold("Как проверить баланс?") == "как проверить баланс"
    # Короткие коды — опознаваемые токены темы, их нельзя терять.
    assert "*100#" in fold("Наберите *100#.")
    assert "*145*" in fold("*145*номер*сумма#")


def test_нормализация_сворачивает_кыргызские_буквы():
    """«көрөм» и «korom» обязаны сойтись в одной записи."""
    assert fold("көрөм") == fold("корom".replace("om", "ом")) == "кором"
    assert fold("Жоголгон SIM") == "жоголгон sim сим"
    assert transliterate("balansymdy") == "балансымды"
    assert "балансымды" in fold("Balansymdy kantip korom")


def test_латиница_и_кириллица_один_термин():
    """Для токенного метода «korom» и «кором» — одно слово, а не два."""
    terms = content_terms("Balansymdy korom")
    assert all(isinstance(term, tuple) for term in terms)
    assert any("балансымды" in term and "balansymdy" in term for term in terms)


def test_служебные_слова_выброшены():
    assert content_text("как у меня") == ""
    assert content_text("Как проверить баланс?") == "проверить баланс"
    assert content_text("Балансты кантип текшерем?") == "балансты текшерем"
    # Отрицание — это суть темы «интернет не работает», его не выбрасываем.
    assert "не" in content_text("Интернет не работает")


def test_индекс_покрывает_все_темы(entries):
    fields = build_fields(entries)
    assert {field.faq_id for field in fields} == {entry.id for entry in entries}
    assert all(field.text.strip() for field in fields)
    assert all(0.0 < field.weight <= 1.0 for field in fields)


def test_загрузка_из_файла_совпадает_с_базой_по_умолчанию(entries):
    assert load_faq(config.FAQ_PATH) == entries
