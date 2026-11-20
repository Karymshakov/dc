"""Учёт неотвеченных вопросов и агрегат для команды /stats.

Пункт 5 ТЗ: показать, сколько вопросов осталось без ответа, какие темы
чаще не находились и примеры формулировок.

Приватность (уточнения организаторов, раздел 5): /stats не раскрывает
чужие персональные данные. Поэтому:

* chat_id не хранится — только необратимый отпечаток, чтобы посчитать
  число разных собеседников;
* текст примера обезличивается: длинные цепочки цифр (номера, суммы
  карт), e-mail и @ники заменяются на «***»;
* наружу отдаётся агрегат и несколько коротких примеров формулировок,
  а не лента чужих сообщений;
* передачи оператору считаются по кодам причин (`record_operator`),
  без текста обращения.

Хранилище — память процесса, ограниченная по числу записей.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from . import config

# Сколько неотвеченных вопросов держим в памяти.
MAX_RECORDS: int = 500
# До какой длины обрезаем пример формулировки.
EXAMPLE_LENGTH: int = 60

_DIGITS_RE = re.compile(r"\d{4,}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE)
_NICK_RE = re.compile(r"(?<!\w)@\w+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+", re.UNICODE)


def sanitize(text: str, limit: int = EXAMPLE_LENGTH) -> str:
    """Обезличить формулировку перед показом в /stats."""
    cleaned = _SPACES_RE.sub(" ", text).strip()
    cleaned = _EMAIL_RE.sub("***", cleaned)
    cleaned = _NICK_RE.sub("***", cleaned)
    cleaned = _DIGITS_RE.sub("***", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


def _fingerprint(chat_id: int | str) -> str:
    """Необратимый отпечаток чата: считаем собеседников, не храня их id."""
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class UnansweredQuestion:
    """Неотвеченный вопрос в обезличенном виде."""

    text: str
    lang: str
    nearest_faq_id: int | None
    confidence: float
    at: float


@dataclass(frozen=True)
class StatsSnapshot:
    """Срез статистики: только агрегат и обезличенные примеры."""

    total: int = 0
    answered: int = 0
    unanswered: int = 0
    chats: int = 0
    by_language: Mapping[str, int] = field(default_factory=dict)
    nearest_topics: tuple[tuple[int, int], ...] = ()
    examples: tuple[str, ...] = ()
    operator_total: int = 0
    operator_reasons: tuple[tuple[str, int], ...] = ()

    @property
    def unanswered_share(self) -> float:
        """Доля неотвеченных в [0, 1]."""
        return self.unanswered / self.total if self.total else 0.0


class StatsCollector:
    """Счётчик вопросов и неотвеченных формулировок."""

    def __init__(self,
                 max_records: int = MAX_RECORDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._unanswered: deque[UnansweredQuestion] = deque(maxlen=max_records)
        self._total = 0
        self._answered = 0
        self._unanswered_total = 0
        self._by_language: Counter[str] = Counter()
        # Короткая история учтённых вопросов: (язык, нашлась ли тема).
        # Нужна, чтобы `rollback` знал, какой счётчик уменьшать. Именно
        # история, а не последнее значение: дозревание ответа отменяет
        # несколько записей подряд, и одного поля на это не хватает.
        self._history: deque[tuple[str, bool]] = deque(maxlen=64)
        self._chats: set[str] = set()
        self._operator_reasons: Counter[str] = Counter()

    def record(self,
               text: str,
               lang: str,
               faq_id: int | None,
               confidence: float = 0.0,
               nearest_faq_id: int | None = None,
               chat_id: int | str | None = None) -> None:
        """Учесть обработанный вопрос. Пустые реплики не считаются."""
        if not text or not text.strip():
            return
        self._total += 1
        self._by_language[lang] += 1
        if chat_id is not None:
            self._chats.add(_fingerprint(chat_id))
        if faq_id is not None:
            self._answered += 1
            self._history.append((lang, True))
            return
        self._history.append((lang, False))
        self._unanswered_total += 1
        self._unanswered.append(UnansweredQuestion(
            text=sanitize(text),
            lang=lang if lang in config.LANGUAGES else config.DEFAULT_LANGUAGE,
            nearest_faq_id=nearest_faq_id,
            confidence=float(confidence),
            at=self._clock(),
        ))

    def rollback(self) -> bool:
        """Отменить последний учтённый вопрос.

        Нужно из-за дозревания ответа: когда абонент дописывает мысль
        второй и третьей репликой, бот пересчитывает ответ по склеенному
        тексту и переписывает своё сообщение. Для него это ОДИН вопрос,
        а `record` к тому моменту успел посчитать каждую часть отдельно.

        Без отмены `/stats` показывал бы втрое больше обращений, чем было,
        а первая часть незаконченной мысли («крч проблема такая») попадала
        бы в примеры неотвеченных — хотя абонент в итоге получил ответ.

        Симметрично `DialogMemory.rollback()`, который по той же причине
        убирает дописку из памяти диалога.

        Возвращает True, если было что отменять.
        """
        if not self._history:
            return False

        language, answered = self._history.pop()
        self._total = max(0, self._total - 1)
        self._by_language[language] -= 1
        if self._by_language[language] <= 0:
            del self._by_language[language]

        if answered:
            self._answered = max(0, self._answered - 1)
        else:
            self._unanswered_total = max(0, self._unanswered_total - 1)
            if self._unanswered:
                self._unanswered.pop()
        return True

    def record_operator(self, reason: str,
                        chat_id: int | str | None = None) -> None:
        """Учесть передачу оператору: только код причины, без текста.

        Ни формулировка обращения, ни chat_id здесь не сохраняются —
        в /stats попадает лишь счётчик по причинам.
        """
        if not reason:
            return
        self._operator_reasons[str(reason)] += 1
        if chat_id is not None:
            self._chats.add(_fingerprint(chat_id))

    def snapshot(self, examples: int = 5, topics: int = 3) -> StatsSnapshot:
        """Агрегат для показа. Не падает на пустой истории."""
        counter: Counter[int] = Counter(
            item.nearest_faq_id for item in self._unanswered
            if item.nearest_faq_id is not None
        )
        recent = list(self._unanswered)[-examples:][::-1]
        return StatsSnapshot(
            total=self._total,
            answered=self._answered,
            unanswered=self._unanswered_total,
            chats=len(self._chats),
            by_language=dict(sorted(self._by_language.items())),
            nearest_topics=tuple(counter.most_common(topics)),
            examples=tuple(item.text for item in recent),
            operator_total=sum(self._operator_reasons.values()),
            operator_reasons=tuple(self._operator_reasons.most_common()),
        )

    def reset(self) -> None:
        """Обнулить статистику (перезапуск бота, тесты)."""
        self._unanswered.clear()
        self._total = 0
        self._answered = 0
        self._unanswered_total = 0
        self._by_language.clear()
        self._chats.clear()
        self._operator_reasons.clear()
