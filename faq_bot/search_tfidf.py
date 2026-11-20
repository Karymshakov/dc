"""Метод А: символьные n-граммы (3–5) + TF-IDF + косинусная близость.

Почему символьные n-граммы, а не слова. Во-первых, кыргызский
агглютинативен: «тарифти», «тарифимди», «тарифке» — разные словоформы
одного корня, и по словам они не совпадают, а по n-граммам совпадают
на «тариф». Во-вторых, n-граммы терпят опечатку: «балнс» и «баланс»
делят большую часть троек. Стемминг и словарь форм для этого не нужны.

Тема представлена не одним длинным документом, а набором коротких полей
(вопрос ru, вопрос ky, каждое ключевое слово, ответы). Оценка темы —
максимум по её полям: короткий запрос должен сравниваться с короткой
формулировкой, иначе косинус размывается длиной склейки.

Чистая стандартная библиотека, внешних зависимостей нет.
"""
from __future__ import annotations

import math
from collections import Counter

from faq_bot.contracts import FaqEntry, Match
from faq_bot.knowledge import Field, build_fields, content_text, default_faq

NGRAM_MIN = 3
NGRAM_MAX = 5

#: Степень IDF. Больше единицы сильнее давит общую лексику («как», «сколько»).
IDF_POWER = 1.0


def char_ngrams(text: str, low: int = NGRAM_MIN, high: int = NGRAM_MAX) -> Counter[str]:
    """Символьные n-граммы длиной от low до high с краевыми пробелами."""
    padded = f" {text} "
    grams: Counter[str] = Counter()
    for size in range(low, high + 1):
        if len(padded) < size:
            break
        for start in range(len(padded) - size + 1):
            grams[padded[start:start + size]] += 1
    return grams


class TfidfSearch:
    """Поиск по косинусной близости TF-IDF векторов символьных n-грамм."""

    name = "tfidf"

    def __init__(self, entries: tuple[FaqEntry, ...] | None = None,
                 ngram_range: tuple[int, int] = (NGRAM_MIN, NGRAM_MAX),
                 idf_power: float = IDF_POWER) -> None:
        self.entries: tuple[FaqEntry, ...] = entries or default_faq()
        self.low, self.high = ngram_range
        self.idf_power = idf_power
        self.fields: tuple[Field, ...] = build_fields(self.entries)
        self.idf: dict[str, float] = self._build_idf()
        self.vectors: list[dict[str, float]] = [
            self._vectorize(field.text) for field in self.fields
        ]

    def _build_idf(self) -> dict[str, float]:
        """Обратная документная частота; документ — ТЕМА, а не поле.

        Считать df по полям нельзя: слой синонимов даёт теме два десятка
        полей, и слово «роуминг» становится «частым», а служебное «сколько»,
        попавшее в одно поле, — «редким». Документ здесь — тема: n-грамма
        редка, если встречается в одной теме из пятнадцати.
        """
        per_topic: dict[int, set[str]] = {}
        for field in self.fields:
            per_topic.setdefault(field.faq_id, set()).update(
                char_ngrams(field.text, self.low, self.high))
        document_frequency: Counter[str] = Counter()
        for grams in per_topic.values():
            document_frequency.update(grams)
        total = len(per_topic) or 1
        return {
            gram: (math.log((1 + total) / (1 + count)) + 1.0) ** self.idf_power
            for gram, count in document_frequency.items()
        }

    def _vectorize(self, text: str) -> dict[str, float]:
        """TF-IDF вектор, нормированный по длине (L2)."""
        grams = char_ngrams(text, self.low, self.high)
        vector = {
            gram: (1.0 + math.log(count)) * self.idf.get(gram, 0.0)
            for gram, count in grams.items()
        }
        vector = {gram: value for gram, value in vector.items() if value > 0.0}
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0.0:
            return {}
        return {gram: value / norm for gram, value in vector.items()}

    def score_all(self, text: str) -> dict[int, float]:
        """Оценка каждой темы в [0, 1]: максимум по её полям."""
        query = self._vectorize(content_text(text))
        if not query:
            return {}
        scores: dict[int, float] = {}
        for field, vector in zip(self.fields, self.vectors):
            if len(query) > len(vector):
                shared = vector.keys() & query.keys()
            else:
                shared = query.keys() & vector.keys()
            if not shared:
                continue
            cosine = sum(query[gram] * vector[gram] for gram in shared)
            value = cosine * field.weight
            if value > scores.get(field.faq_id, 0.0):
                scores[field.faq_id] = value
        return scores

    def search(self, text: str, limit: int = 3) -> list[Match]:
        """Кандидаты по убыванию score; пустой запрос даёт пустой список."""
        scores = self.score_all(text)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [
            Match(faq_id=faq_id, score=max(0.0, min(1.0, score)), method=self.name)
            for faq_id, score in ordered[:max(0, limit)]
            if score > 0.0
        ]
