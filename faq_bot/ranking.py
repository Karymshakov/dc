"""Сведение методов в решение: порог, топ-3, отсев внецелевых вопросов.

Здесь живут три вещи, которые ТЗ требует отдельными пунктами.

1. Сравнение двух методов и выбор лучшего. Методы сравниваются на открытом
   наборе (tune), в боте работает победитель. Сравнение воспроизводится
   командой `python -m faq_bot.ranking` — она печатает таблицу по каждому
   методу, а не только по выбранному.

2. Порог уверенности. Ниже порога тема не выдаётся: faq_id = null, а
   пользователю показываются три похожих вопроса кнопками. У порога две
   цены, и они считаются отдельно: слишком низкий порог раздаёт темы
   вопросам про погоду и кредит, слишком высокий теряет настоящие вопросы.

3. Калибровка confidence. Сырой косинус живёт в своём масштабе, а
   config.CONFIDENCE_THRESHOLD задан в человеческом: 0.35 — «не уверен»,
   0.55 — «уверен». Монотонная кусочно-линейная калибровка переводит один
   масштаб в другой так, чтобы выбранный по tune сырой порог совпал с
   порогом из config. Порядок кандидатов калибровка не меняет.

4. Гейты подтверждения. Похожесть строк — не единственное условие ответа.
   Тема выдаётся, только если в вопросе есть слово, которое знает
   предметная область вообще (FOREIGN_PENALTY) и которое знает ИМЕННО ЭТА
   тема (UNSUPPORTED_PENALTY). Без них живой чат ловил ложные срабатывания
   на общих корнях: «как ты работаешь» получало тему «не работает
   интернет», «как купить симку» — тему «купить гигабайты», «где купить
   билет на поезд» — её же. Гейты одинаковы для обоих методов, поэтому
   сравнение методов остаётся честным: они меряют лексическую опору
   вопроса, а не похожесть, и ни одному методу не дают преимущества.

   Что именно считается подтверждением, решает search_fuzzy.supported_
   topics, и там же лежат два правила, которые порогом не лечатся:
   омонимия («счёт в матче» — не счёт абонента) и согласие действия
   с объектом («сменить номер» — операция, которой в базе нет, хотя
   «сменить» законно принадлежит теме 5, а «номер» теме 12).

Инвариант, на который опирается бот: faq_id не None тогда и только тогда,
когда confidence >= порога.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from faq_bot import config
from faq_bot.contracts import Decision, FaqEntry, Match
from faq_bot.knowledge import content_terms, default_faq
from faq_bot.search_fuzzy import FuzzySearch
from faq_bot.search_tfidf import TfidfSearch

#: Метод по умолчанию. Выбран по сравнению на tune и на двух сочинённых
#: с нуля наборах: символьные n-граммы дают top-1 100% / 95% / 92.5%,
#: токенный метод — 92% / 75% / 82.5%. На незнакомых формулировках разрыв
#: шире, чем на tune: токенный метод теряет слово целиком, если оно
#: написано иначе, а n-граммы видят общую основу. Подробности в отчёте.
DEFAULT_METHOD = "tfidf"

#: Доступные режимы: два метода ТЗ и их линейная смесь (для сравнения).
METHODS = ("tfidf", "fuzzy", "mix")

#: Вес метода А в режиме «mix». Смесь не выиграла у метода А ни на tune,
#: ни на свежих наборах (совпадает с ним вопрос в вопрос), поэтому
#: остаётся предметом сравнения, а не рабочим режимом.
MIX_ALPHA = 0.7

#: Сырой порог. Выбран свипом по сочинённым с нуля наборам, НЕ по tune:
#: на tune порог невыбираем вовсе — там ровно 100% и ноль ошибок на всём
#: интервале 0.10–0.34, то есть открытый набор о пороге не знает ничего.
#: (Прежний комментарий обещал «коридор [0.19, 0.42], целиком разделяющий
#: классы». На свежих вопросах это неверно в обе стороны, см. ниже.)
#:
#: Свип на fresh+fresh2+омонимы (94 вопроса, из них 19 внецелевых). Три
#: цены считаются отдельно и не усредняются: «потеряно настоящих вопросов /
#: протекло внецелевых / выдана неверная тема», дальше top-1 и зачёт жюри
#: (top-1 >= 75%, top-3 >= 90%):
#:
#:     0.10–0.14    1 / 2 / 3    93.6%   зачёт есть
#:     0.16–0.24    1 / 1 / 3    94.7%   зачёт есть
#:     0.26–0.36    2 / 0 / 3    94.7%   зачёт есть  <- плато без протечек
#:     0.38         3 / 0 / 3    93.6%   зачёт есть
#:     0.42         7 / 0 / 3    89.4%   top-3 только за счёт candidates
#:     0.50        22 / 0 / 3    73.4%   зачёта нет
#:
#: Порог стоит в середине плато [0.26, 0.36]: там максимум top-1 и ноль
#: протечек. Понижать его смысла нет — ниже 0.26 top-1 не растёт, зато
#: возвращается протечка «где купить билет на поезд», а затем «ипотеку
#: дадите». Повышать тоже: с 0.38 начинают осыпаться настоящие вопросы,
#: и tune вслед за ними.
#:
#: Разделяющего значения не существует, и это надо знать прямо: два
#: настоящих вопроса (0.11 и 0.29 в шкале confidence — «сколько гигов?»
#: и «выронил симку») лежат ниже самого уверенного внецелевого (0.30).
#: Порог разделяет остальные 92 из 94, а два вопроса не разделит никакой.
#:
#: Хуже того, порогом в принципе не лечится омонимия: «Футболдун эсеби
#: кандай?» (счёт в игре, а не счёт абонента) получал тему «баланс»
#: с уверенностью 0.47 — выше, чем у настоящих вопросов про баланс.
#: Это вылечено знанием, а не числом: knowledge.HOMONYM_CONTEXT. После
#: этого весь класс таких вопросов остаётся отказом на ЛЮБОМ пороге
#: свипа, потому что штраф за неподтверждённую тему не зависит от порога.
#:
#: Цена ошибки асимметрична в пользу верхнего края: у потерянного
#: настоящего вопроса верная тема остаётся в кандидатах, то есть на
#: кнопках и в top-3, а протёкший внецелевой теряет и top-1, и top-3.
RAW_THRESHOLD = 0.30

#: Сырая оценка, с которой ответ считается уверенным («зелёный свет»).
#:
#: Прежние 0.55 были поставлены на глаз и оказались ровно медианой верных
#: ответов: половина правильных ответов показывалась пользователю как
#: неуверенные. Замер по 88 верным ответам (tune+fresh+fresh2): 10-й
#: процентиль 0.43, 25-й 0.50, медиана 0.59.
#:
#: Отделить этой точкой верные ответы от неверных нельзя: самый уверенный
#: неверный ответ имеет 0.57, то есть выше медианы верных. Поэтому точка
#: выбрана по двум другим условиям: (1) выше сырой оценки любого
#: внецелевого вопроса (максимум 0.41 у «где купить билет на поезд»,
#: который до порога не доходит только благодаря гейтам) — цвет ответа
#: не должен зависеть от одного гейта; (2) насколько возможно ниже, пока
#: чистота зелёной зоны не падает. При 0.45 зелёными становятся 86%
#: верных ответов против 61% при 0.55, а чистота зоны не меняется:
#: 98.7% против 98.1% (во обоих случаях в зелёном один неверный ответ).
RAW_CONFIDENT = 0.45

#: Во сколько раз снижается уверенность, если ни одно значимое слово
#: запроса не известно предметной области. Страховка от внецелевого
#: вопроса, случайно похожего на тему: «где купить билет на поезд».
FOREIGN_PENALTY = 0.4

#: Гейт включается только с двух значимых слов. На одном слове он опасен:
#: «гигов» — разговорная форма, которой нет в словаре, но вопрос законный.
FOREIGN_MIN_TERMS = 2

#: Во сколько раз снижается уверенность темы, которую не подтверждает ни
#: одно различающее слово запроса (knowledge.distinctive_terms).
#:
#: Это лечение второй живой ошибки: совпадение по ОДНОМУ ОБЩЕМУ КОРНЮ без
#: подтверждения другими словами темы. «Как ты работаешь» делит с темой 3
#: корень «работа» и больше ничего, «купить симку» делит с темой 7 корень
#: «купить», а слово «симку» у темы 7 не находит ничего. Штраф того же
#: вида, что FOREIGN_PENALTY, и по той же причине: уверенность должна
#: опираться на лексику темы, а не на общий глагол.
#:
#: Штраф, а не обнуление: подтверждения может не быть и у верной темы,
#: если пользователь написал синоним, которого нет ни в базе, ни в слое
#: синонимов. Тогда тема остаётся в кандидатах (кнопки «похожие вопросы»),
#: но сама не выдаётся как ответ.
UNSUPPORTED_PENALTY = 0.4


def calibrate(raw: float) -> float:
    """Сырая оценка метода в шкалу confidence из config.

    Узлы: 0 -> 0, RAW_THRESHOLD -> CONFIDENCE_THRESHOLD,
    RAW_CONFIDENT -> CONFIDENCE_CONFIDENT, 1 -> 1. Между узлами линейно.
    Функция монотонна, поэтому порядок кандидатов сохраняется.
    """
    knots = (
        (0.0, 0.0),
        (RAW_THRESHOLD, config.CONFIDENCE_THRESHOLD),
        (RAW_CONFIDENT, config.CONFIDENCE_CONFIDENT),
        (1.0, 1.0),
    )
    value = max(0.0, min(1.0, float(raw)))
    for (left_raw, left_out), (right_raw, right_out) in pairwise(knots):
        if value <= right_raw:
            span = right_raw - left_raw
            if span <= 0.0:
                return left_out
            share = (value - left_raw) / span
            return left_out + share * (right_out - left_out)
    return 1.0


class Ranker:
    """Выбор темы, уверенность и три кандидата для кнопок."""

    def __init__(self, entries: tuple[FaqEntry, ...] | None = None,
                 method: str = DEFAULT_METHOD,
                 threshold: float = config.CONFIDENCE_THRESHOLD) -> None:
        if method not in METHODS:
            raise ValueError(f"метод {method!r} неизвестен, доступны {METHODS}")
        self.entries: tuple[FaqEntry, ...] = entries or default_faq()
        self.method = method
        self.threshold = threshold
        self.tfidf = TfidfSearch(self.entries)
        self.fuzzy = FuzzySearch(self.entries)

    # --- оценки ------------------------------------------------------------

    def raw_scores(self, text: str) -> dict[int, float]:
        """Сырые оценки тем выбранным методом."""
        if self.method == "tfidf":
            return self.tfidf.score_all(text)
        if self.method == "fuzzy":
            return self.fuzzy.score_all(text)
        tfidf_scores = self.tfidf.score_all(text)
        fuzzy_scores = self.fuzzy.score_all(text)
        return {
            faq_id: MIX_ALPHA * tfidf_scores.get(faq_id, 0.0)
                    + (1.0 - MIX_ALPHA) * fuzzy_scores.get(faq_id, 0.0)
            for faq_id in set(tfidf_scores) | set(fuzzy_scores)
        }

    def _foreign_vocabulary(self, text: str) -> bool:
        """Все значимые слова запроса чужие для предметной области."""
        terms = content_terms(text)
        if len(terms) < FOREIGN_MIN_TERMS:
            return False
        return self.fuzzy.vocabulary_coverage(text) <= 0.0

    def rank(self, text: str, limit: int = config.TOP_K) -> list[Match]:
        """Кандидаты по убыванию уверенности, не длиннее limit.

        score у Match — уже калиброванная уверенность, в одной шкале с
        Decision.confidence и config.CONFIDENCE_THRESHOLD.

        Штрафы применяются ДО сортировки: тема, не подтверждённая ни одним
        различающим словом, не должна стоять первой только потому, что
        у неё выше сырая похожесть. Иначе «как купить симку» остаётся
        на теме 7, даже когда её уверенность уже снижена.
        """
        scores = self.raw_scores(text)
        if not scores:
            return []
        foreign = FOREIGN_PENALTY if self._foreign_vocabulary(text) else 1.0
        supported = self.fuzzy.supported_topics(text)
        confident = {
            faq_id: max(0.0, min(1.0, calibrate(raw) * foreign * (
                1.0 if faq_id in supported else UNSUPPORTED_PENALTY)))
            for faq_id, raw in scores.items()
        }
        ordered = sorted(confident.items(), key=lambda item: (-item[1], item[0]))
        return [Match(faq_id=faq_id, score=score, method=self.method)
                for faq_id, score in ordered[:max(0, limit)]
                if score > 0.0]

    def decide(self, text: str) -> Decision:
        """Итоговое решение: тема или null, уверенность и три кандидата.

        Кандидаты возвращаются и при отказе: именно они идут на кнопки
        «похожие вопросы», и по ним жюри считает top-3.
        """
        matches = self.rank(text, limit=config.TOP_K)
        if not matches:
            return Decision(faq_id=None, confidence=0.0, candidates=[])
        confidence = matches[0].score
        candidates = [match.faq_id for match in matches]
        faq_id = matches[0].faq_id if confidence >= self.threshold else None
        return Decision(faq_id=faq_id, confidence=confidence,
                        candidates=candidates)


_RANKERS: dict[str, Ranker] = {}


def default_ranker(method: str = DEFAULT_METHOD) -> Ranker:
    """Готовый ранжировщик, собранный один раз на процесс."""
    if method not in _RANKERS:
        _RANKERS[method] = Ranker(method=method)
    return _RANKERS[method]


def reset_rankers() -> None:
    """Забыть собранные индексы: база изменилась (обучение в CRM, тесты)."""
    _RANKERS.clear()


def decide(text: str) -> Decision:
    """Решение по вопросу лучшим методом. Точка входа для answer()."""
    return default_ranker().decide(text)


def rank(text: str, limit: int = config.TOP_K) -> list[Match]:
    """Похожие темы по убыванию уверенности. Для кнопок и для контекста."""
    return default_ranker().rank(text, limit=limit)


# --- Сравнение методов -----------------------------------------------------


@dataclass
class MethodScore:
    """Точность одного метода на наборе вопросов."""

    method: str
    total: int = 0
    top1: int = 0
    top3: int = 0
    in_scope: int = 0
    in_scope_top1: int = 0
    in_scope_top3: int = 0
    out_of_scope: int = 0
    out_of_scope_ok: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        def share(hits: int, base: int) -> str:
            return f"{hits / base:6.1%} ({hits}/{base})" if base else "—"

        return (f"{self.method:<6} "
                f"top-1 {share(self.top1, self.total)}  "
                f"top-3 {share(self.top3, self.total)}  "
                f"по базе {share(self.in_scope_top1, self.in_scope)}  "
                f"вне базы {share(self.out_of_scope_ok, self.out_of_scope)}")


def compare_methods(cases: list[tuple[str, str, int | None]],
                    methods: tuple[str, ...] = METHODS) -> list[MethodScore]:
    """Точность каждого метода на одних и тех же вопросах.

    Случай «вне базы» засчитывается, только если тема не выдана вовсе;
    усреднять его с попаданиями нельзя — это ошибки разной природы,
    поэтому они стоят в таблице отдельными колонками.
    """
    reports: list[MethodScore] = []
    for method in methods:
        ranker = Ranker(method=method)
        report = MethodScore(method=method, total=len(cases))
        for case_id, text, gold in cases:
            decision = ranker.decide(text)
            if gold is None:
                report.out_of_scope += 1
                ok = decision.faq_id is None
                report.out_of_scope_ok += ok
                report.top1 += ok
                report.top3 += ok
                if not ok:
                    report.errors.append(
                        f"{case_id} вне базы -> тема {decision.faq_id}"
                        f" ({decision.confidence:.2f})")
                continue
            report.in_scope += 1
            first = decision.faq_id == gold
            in_three = gold in decision.candidates[:config.TOP_K]
            report.top1 += first
            report.top3 += in_three
            report.in_scope_top1 += first
            report.in_scope_top3 += in_three
            if not first:
                report.errors.append(
                    f"{case_id} тема {decision.faq_id} вместо {gold}"
                    f" ({decision.confidence:.2f}, кандидаты {decision.candidates})")
        reports.append(report)
    return reports


def load_cases(path: Path) -> list[tuple[str, str, int | None]]:
    """Вопросы из CSV формата questions_dev.csv."""
    cases: list[tuple[str, str, int | None]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_id = (row.get("faq_id") or "").strip()
            cases.append(((row.get("id") or "").strip(),
                          row.get("text") or "",
                          int(raw_id) if raw_id else None))
    return cases


def main(argv: list[str] | None = None) -> int:
    """Таблица сравнения методов. По умолчанию — по tune."""
    import argparse

    parser = argparse.ArgumentParser(description="Сравнение методов поиска")
    parser.add_argument("--file", default=str(config.TUNE_PATH),
                        help="CSV с вопросами (по умолчанию tune)")
    parser.add_argument("--errors", action="store_true", help="показать ошибки")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"Не найден {path}. Сначала: python tools/split.py")
        return 1

    cases = load_cases(path)
    print(f"Набор: {path.name}, вопросов {len(cases)}, "
          f"порог {config.CONFIDENCE_THRESHOLD:.2f} "
          f"(сырой {RAW_THRESHOLD:.2f})")
    print(f"Один вопрос весит {100 / len(cases):.1f} процентных пункта\n")
    for report in compare_methods(cases):
        print(report.line())
        if args.errors:
            for item in report.errors:
                print(f"    {item}")
    print(f"\nв боте работает метод {DEFAULT_METHOD!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
