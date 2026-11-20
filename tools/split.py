#!/usr/bin/env python3
"""Деление размеченного набора на tune и holdout.

Зачем. `questions_dev.csv` размечен, и подогнать под него 100% точности
тривиально: достаточно добавить в синонимы слова, подсмотренные в нём.
Жюри прогонит ДРУГИЕ 40 формулировок, и подгонка там рассыплется.

Поэтому набор делится один раз фиксированным сидом:

    tune.csv     26 вопросов — на них настраиваем всё
    holdout.csv  14 вопросов — их не открывает никто до финальной приёмки

Деление стратифицировано: доля внецелевых вопросов (faq_id = null) и
баланс языков в обеих частях сохраняются, иначе holdout перестанет быть
похожим на то, что пришлёт жюри.

    python tools/split.py           создать деление
    python tools/split.py --show    показать состав, НЕ раскрывая holdout
"""
from __future__ import annotations

import argparse
import csv
import random
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "05_support"
SOURCE = DATA_DIR / "questions_dev.csv"
TUNE = DATA_DIR / "tune.csv"
HOLDOUT = DATA_DIR / "holdout.csv"

SEED = 20261121
HOLDOUT_SHARE = 0.35
FIELDS = ("id", "text", "lang", "faq_id")


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS))
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDS}
                         for row in rows)


def stratum(row: dict[str, str]) -> str:
    """Группа, внутри которой делим: язык и наличие темы.

    Внецелевых вопросов в наборе мало, и без стратификации они могли бы
    целиком попасть в одну часть — тогда порог отсева было бы не на чем
    настраивать или не на чем проверять.
    """
    in_scope = "scope" if (row.get("faq_id") or "").strip() else "out"
    return f"{(row.get('lang') or 'ru').strip()}-{in_scope}"


def split(rows: list[dict[str, str]]) -> tuple[list[dict], list[dict]]:
    """Стратифицированное деление с фиксированным сидом."""
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        groups.setdefault(stratum(row), []).append(row)

    generator = random.Random(SEED)
    tune: list[dict[str, str]] = []
    holdout: list[dict[str, str]] = []

    for name in sorted(groups):
        members = sorted(groups[name], key=lambda item: item["id"])
        generator.shuffle(members)
        take = round(len(members) * HOLDOUT_SHARE)
        # Из группы в один элемент holdout не забираем: иначе на tune
        # не останется ни одного примера этого типа.
        take = min(take, max(0, len(members) - 1))
        holdout.extend(members[:take])
        tune.extend(members[take:])

    tune.sort(key=lambda item: item["id"])
    holdout.sort(key=lambda item: item["id"])
    return tune, holdout


def describe(rows: list[dict[str, str]]) -> str:
    """Состав части без раскрытия самих вопросов."""
    languages = Counter((row.get("lang") or "").strip() for row in rows)
    out_of_scope = sum(1 for row in rows if not (row.get("faq_id") or "").strip())
    parts = ", ".join(f"{name}: {count}" for name, count in sorted(languages.items()))
    return f"{len(rows)} вопросов ({parts}), вне базы: {out_of_scope}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Деление dev-набора на tune и holdout")
    parser.add_argument("--show", action="store_true",
                        help="показать состав частей, не раскрывая holdout")
    args = parser.parse_args(argv)

    if not SOURCE.exists():
        print(f"Не найден {SOURCE}")
        return 1

    rows = load_rows(SOURCE)
    tune, holdout = split(rows)

    if not args.show:
        write_rows(TUNE, tune)
        write_rows(HOLDOUT, holdout)
        print(f"записано {TUNE.name} и {HOLDOUT.name}")

    print(f"исходный набор : {describe(rows)}")
    print(f"tune           : {describe(tune)}")
    print(f"holdout        : {describe(holdout)}  <- НЕ ОТКРЫВАТЬ до приёмки")
    print()
    print("tune содержит:", ", ".join(row["id"] for row in tune))
    print("holdout: состав намеренно не печатается")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
