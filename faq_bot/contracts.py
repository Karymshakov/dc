"""Границы между модулями.

Файл существует, чтобы три специалиста могли работать параллельно: каждый
пишет свою часть против этих типов, а не ждёт, пока сосед закончит.
Менять его имеет право только дирижёр — это общий договор, а не деталь
реализации.

Поток данных:

    текст -> normalize -> lang        -> язык ответа
          -> normalize -> search_*    -> список Match
                       -> ranking     -> Decision
                       -> context     -> Decision с унаследованной темой
                       -> answer()    -> словарь для жюри
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FaqEntry:
    """Одна запись базы знаний.

    Поля id, question_ru, question_ky, answer_ru, answer_ky приходят из
    faq.json как есть. Поле keywords добавляем мы: это синонимы и
    разговорные формы темы на обоих языках.
    """

    id: int
    question_ru: str
    question_ky: str
    answer_ru: str
    answer_ky: str
    keywords: tuple[str, ...] = ()

    def question(self, language: str) -> str:
        return self.question_ky if language == "ky" else self.question_ru

    def answer(self, language: str) -> str:
        return self.answer_ky if language == "ky" else self.answer_ru

    def searchable(self) -> str:
        """Весь текст темы, по которому идёт поиск."""
        parts = (self.question_ru, self.question_ky,
                 self.answer_ru, self.answer_ky, *self.keywords)
        return " ".join(parts)


@dataclass(frozen=True)
class Match:
    """Кандидат, найденный одним методом поиска."""

    faq_id: int
    score: float      # строго в [0, 1]
    method: str       # "tfidf" | "fuzzy" | ...


@dataclass
class Decision:
    """Итог поиска до оформления в ответ пользователю."""

    faq_id: int | None
    confidence: float
    candidates: list[int] = field(default_factory=list)
    inherited_from_context: bool = False

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.candidates = [int(item) for item in self.candidates][:3]


@runtime_checkable
class SearchMethod(Protocol):
    """Любой метод поиска по базе.

    Контракт: возвращает кандидатов, упорядоченных по убыванию score,
    не длиннее limit, со score строго в [0, 1]. На пустом или
    бессмысленном запросе возвращает пустой список, а не исключение.
    """

    name: str

    def search(self, text: str, limit: int = 3) -> list[Match]:
        ...


@runtime_checkable
class LanguageDetector(Protocol):
    """Определение языка вопроса.

    Контракт: всегда возвращает "ru" или "ky". Третьего значения нет,
    исключений не бросает. Пустая строка даёт "ru".
    """

    def detect(self, text: str) -> str:
        ...
