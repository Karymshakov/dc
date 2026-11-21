"""Объяснение решения: почему бот ответил именно так.

Бот поддержки, который не может объяснить свой ответ, — чёрный ящик и
для абонента, и для того, кто его сопровождает. Здесь тот же путь, что
проходит `answer()`, разложен на шаги, которые можно показать человеку:

1. как определён язык и по каким уликам;
2. что предложил каждый из двух методов поиска (TF-IDF и нечёткий);
3. какая уверенность получилась после калибровки и штрафов;
4. как она соотносится с порогами и что бот в итоге сделал.

Модуль ничего не решает сам: он вызывает те же `lang` и `ranking`, что и
ядро, поэтому объяснение не может разойтись с настоящим ответом.
Контекст диалога сюда не входит — разбирается один вопрос сам по себе.

Используется командой `/why` в боте и ключом `--why` в `main.py ask`.
Зависимостей, сети и Telegram не требует.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, knowledge, lang, ranking
from .contracts import Decision, FaqEntry, Match

__all__ = ["Explanation", "MethodVote", "explain", "render"]

BAR_WIDTH = 12
MAX_SIGNALS = 6

_LANGUAGE_NAMES = {"ru": "русский", "ky": "кыргызский"}
_METHOD_NAMES = {
    "tfidf": "TF-IDF по символьным n-граммам",
    "fuzzy": "нечёткий поиск по словам",
}
_METHOD_SHORT = {"tfidf": "TF-IDF", "fuzzy": "нечёткий", "mix": "смесь методов"}


@dataclass(frozen=True)
class MethodVote:
    """Лучшая тема одного метода поиска и её сырая оценка."""

    method: str
    faq_id: int | None
    raw: float


@dataclass(frozen=True)
class Explanation:
    """Все промежуточные результаты одного вопроса."""

    text: str
    normalized: str
    verdict: lang.LanguageVerdict
    votes: tuple[MethodVote, ...]
    candidates: tuple[Match, ...]
    decision: Decision

    @property
    def methods_agree(self) -> bool:
        """Оба метода назвали одну и ту же тему."""
        return len({vote.faq_id for vote in self.votes}) == 1

    @property
    def level(self) -> str:
        """Режим светофора — те же пороги, что у `Reply.level`."""
        if self.decision.faq_id is None:
            return "red"
        if self.decision.confidence >= config.CONFIDENCE_CONFIDENT:
            return "green"
        return "yellow"


def _vote(method: str, scores: dict[int, float]) -> MethodVote:
    if not scores:
        return MethodVote(method, None, 0.0)
    faq_id, raw = min(scores.items(), key=lambda item: (-item[1], item[0]))
    return MethodVote(method, faq_id if raw > 0.0 else None, raw)


def explain(text: str) -> Explanation:
    """Разобрать вопрос по шагам. На пустом входе — пустое объяснение."""
    raw = text[:config.MAX_INPUT_CHARS] if isinstance(text, str) else ""
    verdict = lang.DETECTOR.explain(raw)
    if not raw.strip():
        return Explanation(raw, "", verdict, (), (),
                           Decision(faq_id=None, confidence=0.0, candidates=[]))

    ranker = ranking.default_ranker()
    votes = (
        _vote("tfidf", ranker.tfidf.score_all(raw)),
        _vote("fuzzy", ranker.fuzzy.score_all(raw)),
    )
    return Explanation(
        text=raw,
        normalized=knowledge.content_text(raw),
        verdict=verdict,
        votes=votes,
        candidates=tuple(ranker.rank(raw, limit=config.TOP_K)),
        decision=ranker.decide(raw),
    )


# --------------------------------------------------------------------------
# Текст для человека
# --------------------------------------------------------------------------

def _bar(value: float) -> str:
    filled = round(max(0.0, min(1.0, value)) * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _title(faq_id: int | None, entries: dict[int, FaqEntry], language: str) -> str:
    if faq_id is None:
        return "ничего не нашёл"
    entry = entries.get(faq_id)
    name = entry.question(language) if entry is not None else ""
    return f"тема {faq_id} «{name}»" if name else f"тема {faq_id}"


def _verdict_line(explanation: Explanation) -> str:
    decision = explanation.decision
    if explanation.level == "green":
        return (f"🟢 {decision.confidence:.2f} ≥ {config.CONFIDENCE_CONFIDENT:.2f}"
                f", отвечаем темой {decision.faq_id}")
    if explanation.level == "yellow":
        return (f"🟡 {decision.confidence:.2f} между порогами, отвечаем темой "
                f"{decision.faq_id} и предлагаем соседние")
    return (f"🔴 {decision.confidence:.2f} < {config.CONFIDENCE_THRESHOLD:.2f}"
            ", не отвечаем, показываем три похожие темы кнопками")


def render(explanation: Explanation,
           entries: tuple[FaqEntry, ...] | None = None,
           inherited_from_context: bool = False) -> str:
    """Объяснение простым текстом: одинаково для бота и для терминала.

    `inherited_from_context` — бот в диалоге взял тему из предыдущей
    реплики; сам вопрос при этом разбирается как отдельный.
    """
    if not explanation.text.strip():
        return "Вопрос пустой: язык по умолчанию ru, тема не выбрана."

    by_id = {entry.id: entry for entry in (entries or knowledge.default_faq())}
    verdict = explanation.verdict
    language = verdict.language

    lines = [
        "🔍 Почему бот ответил так",
        "",
        f"Вопрос: «{explanation.text.strip()}»",
        f"Так его видит поиск: {explanation.normalized or 'ничего'}",
        "",
        f"1. Язык: {_LANGUAGE_NAMES.get(language, language)} "
        f"(улики ky {verdict.ky_score:g} : ru {verdict.ru_score:g})",
    ]
    signals = verdict.signals[:MAX_SIGNALS]
    lines += [f"   • {signal}" for signal in signals]
    if len(verdict.signals) > len(signals):
        lines.append(f"   • …и ещё {len(verdict.signals) - len(signals)}")
    if not verdict.signals:
        lines.append("   • улик нет, язык по умолчанию")

    lines += ["", "2. Два метода поиска"]
    for vote in explanation.votes:
        lines.append(f"   • {_METHOD_NAMES.get(vote.method, vote.method)}: "
                     f"{_title(vote.faq_id, by_id, language)} ({vote.raw:.2f})")
    lines.append("   Методы согласны." if explanation.methods_agree else
                 "   Методы разошлись, в боте работает "
                 f"{_METHOD_SHORT.get(ranking.DEFAULT_METHOD, ranking.DEFAULT_METHOD)}: "
                 "он точнее на открытом наборе.")

    lines += ["", "3. Уверенность после калибровки и штрафов"]
    for match in explanation.candidates:
        lines.append(f"   {_bar(match.score)} {match.score:.2f}  "
                     f"{_title(match.faq_id, by_id, language)}")
    if not explanation.candidates:
        lines.append("   ни одна тема не набрала ничего")
    lines.append(f"   пороги: ответ с {config.CONFIDENCE_THRESHOLD:.2f}, "
                 f"уверенный ответ с {config.CONFIDENCE_CONFIDENT:.2f}")

    lines += ["", f"4. Решение: {_verdict_line(explanation)}"]
    if inherited_from_context:
        lines.append("   В диалоге этот вопрос был уточняющим: бот взял тему "
                     "из предыдущей реплики.")
    return "\n".join(lines)
