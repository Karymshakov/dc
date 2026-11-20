#!/usr/bin/env python3
"""Измерение точности: top-1, top-3, язык, отсев внецелевых вопросов.

Это общий инструмент всей бригады. Любое утверждение вида «стало лучше»
принимается только вместе с его выводом до и после.

    python tools/evaluate.py                      по tune
    python tools/evaluate.py --split holdout      ФИНАЛЬНАЯ приёмка, один раз
    python tools/evaluate.py --file my.csv        по своему набору
    python tools/evaluate.py --errors             показать ошибки

Дисциплина измерений. В tune 25 вопросов, поэтому один вопрос — это
4 процентных пункта. Прирост меньше двух пунктов не является улучшением,
это шум. Инструмент печатает рядом с процентом абсолютное число, чтобы
об этом нельзя было забыть.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from faq_bot import config  # noqa: E402
from faq_bot.answer import answer  # noqa: E402


@dataclass
class Case:
    id: str
    text: str
    lang: str
    faq_id: int | None


@dataclass
class Outcome:
    case: Case
    predicted_lang: str
    predicted_faq: int | None
    candidates: list[int]
    confidence: float

    @property
    def lang_ok(self) -> bool:
        return self.predicted_lang == self.case.lang

    @property
    def top1_ok(self) -> bool:
        return self.predicted_faq == self.case.faq_id

    @property
    def top3_ok(self) -> bool:
        if self.case.faq_id is None:
            # Для внецелевого вопроса «попасть в топ-3» означает
            # не предложить тему вовсе.
            return self.predicted_faq is None
        return self.case.faq_id in self.candidates[:config.TOP_K]


@dataclass
class Report:
    total: int = 0
    lang_ok: int = 0
    top1_ok: int = 0
    top3_ok: int = 0
    in_scope: int = 0
    in_scope_ok: int = 0
    out_of_scope: int = 0
    out_of_scope_ok: int = 0
    outcomes: list[Outcome] = field(default_factory=list)

    def share(self, hits: int) -> str:
        if not self.total:
            return "—"
        return f"{hits / self.total:6.1%} ({hits}/{self.total})"


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_id = (row.get("faq_id") or "").strip()
            cases.append(Case(
                id=(row.get("id") or "").strip(),
                text=row.get("text") or "",
                lang=(row.get("lang") or config.DEFAULT_LANGUAGE).strip(),
                faq_id=int(raw_id) if raw_id else None,
            ))
    return cases


def evaluate(cases: list[Case]) -> Report:
    report = Report(total=len(cases))
    for case in cases:
        result = answer(case.text)
        outcome = Outcome(
            case=case,
            predicted_lang=result.get("lang", ""),
            predicted_faq=result.get("faq_id"),
            candidates=list(result.get("candidates") or []),
            confidence=float(result.get("confidence") or 0.0),
        )
        report.outcomes.append(outcome)

        report.lang_ok += outcome.lang_ok
        report.top1_ok += outcome.top1_ok
        report.top3_ok += outcome.top3_ok

        if case.faq_id is None:
            report.out_of_scope += 1
            report.out_of_scope_ok += outcome.predicted_faq is None
        else:
            report.in_scope += 1
            report.in_scope_ok += outcome.top1_ok
    return report


def verdict(value: float, threshold: float) -> str:
    return "OK  " if value >= threshold else "НИЖЕ"


def print_report(report: Report, name: str, show_errors: bool) -> None:
    print(f"Набор: {name}")
    print()
    top1 = report.top1_ok / report.total if report.total else 0.0
    top3 = report.top3_ok / report.total if report.total else 0.0
    lang = report.lang_ok / report.total if report.total else 0.0

    print(f"  top-1 (верная тема первой) : {report.share(report.top1_ok)}"
          f"   порог {config.JURY_TOP1:.0%}  {verdict(top1, config.JURY_TOP1)}")
    print(f"  top-3 (верная тема в трёх) : {report.share(report.top3_ok)}"
          f"   порог {config.JURY_TOP3:.0%}  {verdict(top3, config.JURY_TOP3)}")
    print(f"  язык определён верно       : {report.share(report.lang_ok)}"
          f"   порог {config.JURY_LANG:.0%}  {verdict(lang, config.JURY_LANG)}")
    print()
    if report.in_scope:
        print(f"  вопросы по базе     : {report.in_scope_ok}/{report.in_scope} найдено")
    if report.out_of_scope:
        print(f"  вопросы вне базы    : {report.out_of_scope_ok}/{report.out_of_scope}"
              f" верно отсеяно (ожидается faq_id = null)")
    print()
    print(f"  один вопрос в этом наборе весит "
          f"{100 / report.total if report.total else 0:.1f} процентных пункта")

    if show_errors:
        wrong = [item for item in report.outcomes
                 if not item.top1_ok or not item.lang_ok]
        if not wrong:
            print("\n  ошибок нет")
            return
        print(f"\n  ОШИБКИ ({len(wrong)}):")
        for item in wrong:
            marks = []
            if not item.top1_ok:
                marks.append(f"тема {item.predicted_faq} вместо {item.case.faq_id}")
            if not item.lang_ok:
                marks.append(f"язык {item.predicted_lang} вместо {item.case.lang}")
            print(f"    {item.case.id}  «{item.case.text}»")
            print(f"        {'; '.join(marks)}  (уверенность {item.confidence:.2f},"
                  f" кандидаты {item.candidates})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Точность поиска ответа в FAQ")
    parser.add_argument("--split", choices=("tune", "holdout", "dev"), default="tune",
                        help="какой набор мерить (holdout — только финальная приёмка)")
    parser.add_argument("--file", default=None, help="свой CSV вместо набора")
    parser.add_argument("--errors", action="store_true", help="показать ошибки")
    args = parser.parse_args(argv)

    if args.file:
        path, name = Path(args.file), args.file
    else:
        path = {"tune": config.TUNE_PATH,
                "holdout": config.HOLDOUT_PATH,
                "dev": config.DEV_PATH}[args.split]
        name = args.split
        if args.split == "holdout":
            print("ВНИМАНИЕ: holdout предназначен для финальной приёмки.")
            print("Настройка по нему обесценивает метрику.\n")

    if not path.exists():
        print(f"Не найден {path}. Сначала: python tools/split.py")
        return 1

    report = evaluate(load_cases(path))
    print_report(report, name, args.errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
