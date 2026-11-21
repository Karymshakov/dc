"""Обучение бота на отметках операторов — с проверкой до применения.

Оператор видит реплику, которую бот не понял, и отмечает: «это тема 1».
Формулировка становится синонимом темы 1, и следующий абонент с таким же
вопросом получает ответ сразу.

Три правила, ради которых модуль существует отдельно:

1. **Учим формулировкам, не фактам.** Выученная фраза только помогает
   найти тему. Ответ по-прежнему берётся из faq.json дословно, поэтому
   опечатка или неверная цена оператора в бота не попадают. Свободные
   ответы операторов идут в очередь предложений (`proposals`) и в бота
   сами не попадают никогда.

2. **Ошибка оператора не портит бота.** Перед применением фраза
   проверяется на копии базы: открытый набор `questions_dev.csv`
   прогоняется до и после. Если top-1 или top-3 упали хоть на один
   вопрос — фраза отклоняется с объяснением.

3. **Никакой подгонки под открытый набор.** Фраза, совпадающая с вопросом
   из questions_dev.csv, не принимается: тот же запрет, что и для ручного
   слоя синонимов (tests/test_knowledge.py).
"""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from faq_bot import config, knowledge

MIN_CHARS = 3
MAX_CHARS = 160

_SPACES = re.compile(r"\s+")


@dataclass
class Score:
    top1: int = 0
    top3: int = 0
    total: int = 0

    @property
    def top1_share(self) -> float:
        return self.top1 / self.total if self.total else 0.0

    @property
    def top3_share(self) -> float:
        return self.top3 / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"top1": self.top1, "top3": self.top3, "total": self.total,
                "top1_share": round(self.top1_share, 4),
                "top3_share": round(self.top3_share, 4)}


@dataclass
class Verdict:
    """Итог проверки фразы. `code` машиночитаем, `message` — для оператора."""

    accepted: bool
    code: str
    message: str
    phrase: str
    faq_id: int | None
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    recognized_as: int | None = None
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean(phrase: str) -> str:
    return _SPACES.sub(" ", (phrase or "").strip())


def _cases() -> list[tuple[str, int | None]]:
    from faq_bot.ranking import load_cases

    # load_cases отдаёт (id вопроса, текст, faq_id).
    return [(text, expected) for _qid, text, expected in load_cases(config.DEV_PATH)]


def score(ranker: Any, cases: list[tuple[str, int | None]]) -> Score:
    """top-1 и top-3 на наборе; вопрос вне базы засчитывается за null."""
    result = Score(total=len(cases))
    for text, expected in cases:
        decision = ranker.decide(text)
        if decision.faq_id == expected:
            result.top1 += 1
        if expected is None:
            result.top3 += decision.faq_id is None
        elif expected in decision.candidates[:config.TOP_K]:
            result.top3 += 1
    return result


def _ranker(learned: dict[int, tuple[str, ...]]) -> Any:
    from faq_bot.ranking import Ranker

    return Ranker(entries=knowledge.load_faq(learned=learned))


class Learner:
    """Выученный слой: проверка, применение, откат."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cases: list[tuple[str, int | None]] | None = None
        self._dev_folded: set[str] | None = None
        # Ранжировщик и точность для текущего слоя: «до» не пересчитывается
        # на каждой отметке, пока слой не изменился.
        self._baseline: tuple[tuple, Any, Score] | None = None

    def _before(self, current: dict[int, tuple[str, ...]]) -> tuple[Any, Score]:
        key = tuple(sorted((k, tuple(v)) for k, v in current.items()))
        if self._baseline is None or self._baseline[0] != key:
            ranker = _ranker(current)
            self._baseline = (key, ranker, score(ranker, self.cases()))
        return self._baseline[1], self._baseline[2]

    def cases(self) -> list[tuple[str, int | None]]:
        if self._cases is None:
            self._cases = _cases()
        return self._cases

    def _is_dev_question(self, phrase: str) -> bool:
        if self._dev_folded is None:
            self._dev_folded = {knowledge.content_text(text)
                                for text, _ in self.cases()}
        return knowledge.content_text(phrase) in self._dev_folded

    def check(self, phrase: str, faq_id: int,
              current: dict[int, tuple[str, ...]]) -> Verdict:
        """Проверить фразу на копии базы. Рабочий бот не меняется."""
        phrase = clean(phrase)
        base = dict(phrase=phrase, faq_id=faq_id)
        known_ids = {entry.id for entry in knowledge.default_faq()}
        if faq_id not in known_ids:
            return Verdict(False, "unknown_topic", "Такой темы в базе нет.", **base)
        if not MIN_CHARS <= len(phrase) <= MAX_CHARS:
            return Verdict(False, "length",
                           f"Фраза должна быть от {MIN_CHARS} до {MAX_CHARS} знаков.",
                           **base)
        if not knowledge.content_terms(phrase):
            return Verdict(False, "no_terms",
                           "В фразе нет значимых слов, учить нечему.", **base)
        if self._is_dev_question(phrase):
            return Verdict(False, "dev_question",
                           "Фраза совпадает с вопросом открытого набора. "
                           "Учить на нём, подгонка под проверку.", **base)
        if any(knowledge.content_text(item) == knowledge.content_text(phrase)
               for item in current.get(faq_id, ())):
            return Verdict(False, "duplicate", "Эта фраза уже выучена.", **base)

        cases = self.cases()
        before_ranker, before = self._before(current)
        known = before_ranker.decide(phrase)
        if known.faq_id == faq_id:
            return Verdict(False, "already_known",
                           "Бот уже отвечает на эту фразу этой темой.",
                           recognized_as=known.faq_id,
                           confidence=round(known.confidence, 3), **base)

        candidate = {key: tuple(value) for key, value in current.items()}
        candidate[faq_id] = candidate.get(faq_id, ()) + (phrase,)
        after_ranker = _ranker(candidate)
        after = score(after_ranker, cases)
        decided = after_ranker.decide(phrase)
        common = dict(before=before.as_dict(), after=after.as_dict(),
                      recognized_as=decided.faq_id,
                      confidence=round(decided.confidence, 3), **base)

        if after.top1 < before.top1 or after.top3 < before.top3:
            lost = (before.top1 - after.top1) or (before.top3 - after.top3)
            return Verdict(False, "regression",
                           f"Фраза сбивает бота на других вопросах: точность "
                           f"на открытом наборе упала бы на {lost} из {before.total}. "
                           "Похоже, тема выбрана неверно.", **common)
        if decided.faq_id != faq_id:
            return Verdict(True, "accepted_weak",
                           "Принято. Сама фраза пока узнаётся неуверенно, "
                           "ещё пара похожих отметок закрепит тему.", **common)
        return Verdict(True, "accepted",
                       "Принято: бот теперь отвечает на это темой "
                       f"{faq_id}, точность на открытом наборе не упала.", **common)

    @staticmethod
    def apply(mapping: dict[int, tuple[str, ...]]) -> None:
        """Включить слой в рабочем боте: сбросить кэши базы и ранжировщика."""
        from faq_bot import answer

        knowledge.set_learned(mapping)
        answer.reload_knowledge()

    @staticmethod
    def mapping(rows: list[dict[str, Any]]) -> dict[int, tuple[str, ...]]:
        result: dict[int, list[str]] = {}
        for row in sorted(rows, key=lambda item: item["id"]):
            result.setdefault(int(row["faq_id"]), []).append(row["phrase"])
        return {key: tuple(value) for key, value in result.items()}

    @property
    def lock(self) -> threading.Lock:
        return self._lock
