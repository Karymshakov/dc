"""Приёмка на вопросах, сочинённых с нуля.

Зачем отдельный файл. `tune` — набор, по которому настраивались, на нём
100%, и эта цифра не значит ничего: любую систему можно довести до
единицы на наборе, который видно. Жюри прогонит другие формулировки,
поэтому главная проверка проекта — здесь.

Три набора в `data/05_support/independent/` отличаются характером:

* `01_spoken_ru.csv` — разговорный русский, как пишут в поддержку:
  без заглавных, без знаков, с проглоченными словами;
* `02_kyrgyz.csv` — кыргызский, включая латиницу и агглютинацию;
* `03_edge.csv` — край: однословные запросы, формулировки рядом с
  предметной областью, которые обязаны получить отказ.

Третий набор сочинён враждебно и намеренно: он должен ломать решение, а
не подтверждать его. Поэтому порог ниже именно на нём.

Что проверяется, а что нет
--------------------------

Проверяются **пороги жюри** (75 / 90 / 90), а не достигнутые значения.
Это важно. Тест на «не хуже, чем сейчас» превратил бы независимый набор
в ещё одну цель подгонки: достаточно подкрутить словарь под эти 124
вопроса, и набор перестанет что-либо измерять. Текущие значения живут в
README как замер на дату, а тест охраняет только порог.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from faq_bot.answer import all_entries, answer
from faq_bot.knowledge import KEYWORDS
from faq_bot.normalize import normalize

DATA = Path(__file__).resolve().parents[1] / "data" / "05_support"
INDEPENDENT = DATA / "independent"
LABELLED = ("questions_dev.csv", "tune.csv", "holdout.csv")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sets() -> list[Path]:
    return sorted(INDEPENDENT.glob("0*.csv"))


def _measure(paths: list[Path]) -> tuple[int, int, int, int]:
    """Вернуть (всего, top-1, top-3, язык)."""
    total = top1 = top3 = lang = 0
    for path in paths:
        for row in _rows(path):
            raw = (row.get("faq_id") or "").strip()
            expected = int(raw) if raw else None
            result = answer(row["text"])
            total += 1
            if result["faq_id"] == expected:
                top1 += 1
            candidates = list(result.get("candidates") or [])
            if expected is None:
                # Для внецелевого вопроса «в тройке» означает одно:
                # темы в ответе нет. Иначе отказ зачёлся бы дважды.
                if result["faq_id"] is None:
                    top3 += 1
            elif expected in candidates[:3]:
                top3 += 1
            if result["lang"] == row["lang"]:
                lang += 1
    return total, top1, top3, lang


def test_sets_exist_and_hold_124_questions():
    """Набор лежит в репозитории: иначе цифру нельзя показать жюри."""
    paths = _sets()

    assert len(paths) == 3, "ожидаются три набора разного характера"
    assert sum(len(_rows(p)) for p in paths) == 124


def test_not_a_single_question_repeats_the_labelled_data():
    """Совпадение с размеченным вопросом обесценивает весь замер.

    Проверка уже отработала: при составлении наборов она нашла четыре
    формулировки, которые дословно повторяли размеченные данные или
    вопрос самой базы. Все четыре заменены.
    """
    labelled = {normalize(row["text"])
                for name in LABELLED for row in _rows(DATA / name)}
    for entry in all_entries():
        labelled.add(normalize(entry.question("ru")))
        labelled.add(normalize(entry.question("ky")))

    repeats = [(path.name, row["id"], row["text"])
               for path in _sets() for row in _rows(path)
               if normalize(row["text"]) in labelled]

    assert repeats == []


def test_no_question_was_copied_into_the_synonym_layer():
    """Обратная утечка: вопрос набора нельзя переписать в словарь.

    Без этой проверки «улучшение» выглядело бы так: взять промах из
    независимого набора, добавить его слова в синонимы темы — и цифра
    выросла, не изменив ничего для абонента с другой формулировкой.
    Проверка уже отработала: `Сайттар ачылбай жатат` дословно лежало в
    словаре темы 3, то есть измеряло наличие записи в словаре, а не
    работу поиска. Формулировка заменена.

    Однословные запросы из правила исключены. «баланс», «роуминг»,
    «apn», «детализация» совпадают с ключевыми словами по природе вещей:
    это и есть название услуги, другого слова для неё нет. Запрет на них
    означал бы запрет проверять самый частый вид запроса.
    """
    synonyms = {normalize(word)
                for words in KEYWORDS.values() for word in words}

    copied = [(path.name, row["id"]) for path in _sets()
              for row in _rows(path)
              if len(row["text"].split()) > 1
              and normalize(row["text"]) in synonyms]

    assert copied == []


@pytest.mark.parametrize("name, floor", [
    ("01_spoken_ru.csv", 0.75),
    ("02_kyrgyz.csv", 0.75),
    # Край сочинён враждебно: однословные запросы и формулировки рядом с
    # предметной областью. Порог жюри он не держит и не обязан — он
    # нужен, чтобы видеть границу, а не чтобы её скрывать.
    ("03_edge.csv", 0.60),
])
def test_each_set_holds_its_floor(name, floor):
    total, top1, _, _ = _measure([INDEPENDENT / name])

    assert top1 / total >= floor, f"{name}: top-1 {top1 / total:.1%}"


def test_all_124_together_pass_the_jury_thresholds():
    """Главная цифра проекта. Пороги жюри, а не достигнутые значения."""
    total, top1, top3, lang = _measure(_sets())

    assert top1 / total >= 0.75, f"top-1 {top1 / total:.1%}"
    assert top3 / total >= 0.90, f"top-3 {top3 / total:.1%}"
    assert lang / total >= 0.90, f"язык {lang / total:.1%}"
