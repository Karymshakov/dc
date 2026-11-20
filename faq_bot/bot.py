"""Telegram-бот поверх `answer()`.

Бот — надстройка и демонстрация, а не ядро: автопроверка жюри идёт по
функции `answer()`. Поэтому `python-telegram-bot` импортируется **лениво**,
внутри функций, в момент запуска. Модуль спокойно импортируется и
тестируется там, где библиотеки нет вовсе.

Токен берётся из переменной окружения `TELEGRAM_BOT_TOKEN` и в репозиторий
не попадает. Без токена запуск печатает понятное сообщение и выходит с
кодом 2, без трассировки стека.

Вся логика экранов собрана в двух чистых функциях — `screen_for_text()` и
`screen_for_action()`. Они не знают про Telegram, поэтому поведение кнопок
проверяется обычными тестами.

Главное правило переписки, из которого следует всё остальное:

* **первая реплика мысли** — новое сообщение в чат (`_send()`);
* **нажатие кнопки** — правка того сообщения, в котором эта кнопка
  (`_edit()` поверх `edit_message_text`), плюс обязательный
  `answer_callback_query`, чтобы на кнопке не крутились часики;
* **реплика, дописанная к своему же вопросу** — правка прежнего ответа
  (`_rewrite()`), пересчитанного по склеенному тексту.

До этого каждое касание присылало новое сообщение — «Понятно» давало
подтверждение и следом полное меню, то есть три сообщения на одно
касание. За минуту живого теста чат превращался в простыню из двадцати
сообщений, в которой не найти сам ответ.

Чем занимается именно этот слой (и не занимается `answer()`).

* **Защита от флуда.** `ThrottleGuard` ограничивает число ответов на чат
  в окно времени, а слишком длинное сообщение не разбирается как вопрос.
* **Ход разговора.** `ConversationFlow` отвечает на первую реплику
  сразу, а дописанные уточнения не присылает отдельными сообщениями, а
  переписывает ими прежний ответ. Он же помнит, какую тему бот уже
  показывал, и распознаёт «этот совет не помог»: повторять тот же текст
  бот не имеет права, он предлагает другие темы и живого оператора.
* **Приветствия и болтовня.** «салам», «кто ты», «рахмат» получают
  человеческий ответ здесь, до вызова `answer()`. Сама функция
  `answer()` по-прежнему отдаёт на такие реплики `faq_id = null` —
  так требует спецификация, и по ней считается автопроверка.
* **Голос.** Голосовое сообщение скачивается, распознаётся в
  `faq_bot.speech` и дальше идёт обычным путём, с эхом услышанного перед
  карточкой. Пределы длины и бюджеты живут в самом `speech`: ключ
  оплачивает человек, и этот слой их не ослабляет.
* **Язык.** Выбор языка кнопкой сохраняется для чата и перебивает
  автоопределение; правило спора описано у `reply_language()`. Экран
  печатается на одном языке, а кнопка 🇰🇬/🇷🇺 перерисовывает его же на
  другом — см. `_language_screen()`.

Тексты ответов, списки слов и пороги лежат в `faq_bot/bot_rules.json`
и читаются через `presentation.rules()`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, handoff, presentation
from .answer import (
    MEMORY,
    STATS,
    Reply,
    all_entries,
    detect_language,
    entry_by_id,
    reply_for_faq_id,
    reset_context,
    respond,
    stats_snapshot,
)
from .context import mentions_topic
from .contracts import FaqEntry
from .presentation import Screen

logger = logging.getLogger(__name__)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# Насколько улики другого языка должны перевесить выбор кнопкой, чтобы
# реплика считалась «явно написанной на другом языке». Три очка — вес
# одного служебного слова из закрытого списка faq_bot.lang, то есть одна
# надёжная улика, а не случайное совпадение.
LANGUAGE_EVIDENCE_MARGIN: float = 3.0

# Сколько таких реплик подряд снимают выбор языка кнопкой. Одна реплика
# может быть цитатой или копипастой, две подряд — человек действительно
# перешёл на другой язык.
LANGUAGE_RELEASE_STREAK: int = 2

NO_TOKEN_MESSAGE = (
    "Не задан токен бота.\n"
    "\n"
    "Токен не хранится в репозитории. Положите его в переменную окружения:\n"
    "  PowerShell : $env:TELEGRAM_BOT_TOKEN = \"<токен от @BotFather>\"\n"
    "  bash       : export TELEGRAM_BOT_TOKEN='<токен от @BotFather>'\n"
    "\n"
    "Проверка ответов токена не требует:\n"
    "  python main.py ask \"Как проверить баланс?\"\n"
    "  python tools/evaluate.py"
)

NO_LIBRARY_MESSAGE = (
    "Не установлена библиотека python-telegram-bot.\n"
    "\n"
    "  pip install \"python-telegram-bot>=20\"\n"
    "\n"
    "Она нужна только боту. Функция answer(), команды ask и eval\n"
    "работают без неё."
)

# Язык интерфейса по чатам: последний определённый или выбранный кнопкой.
_chat_language: dict[str, str] = {}
# Чаты, где язык выбран кнопкой вручную: автоопределение больше не спорит.
_chat_language_pinned: set[str] = set()
# Сколько реплик подряд в чате явно написаны на другом языке.
_chat_language_streak: dict[str, int] = {}


def _key(chat_id: int | str | None) -> str:
    return str(chat_id)


# --------------------------------------------------------------------------
# Язык чата
# --------------------------------------------------------------------------

def chat_language(chat_id: int | str | None) -> str:
    """Язык интерфейса этого чата."""
    return _chat_language.get(_key(chat_id), config.DEFAULT_LANGUAGE)


def is_language_pinned(chat_id: int | str | None) -> bool:
    """Выбран ли язык этого чата кнопкой."""
    return _key(chat_id) in _chat_language_pinned


def set_chat_language(chat_id: int | str | None, language: str,
                      pinned: bool = False) -> None:
    """Запомнить язык чата. `pinned` — выбор кнопкой, автоопределение молчит."""
    if language not in config.LANGUAGES:
        return
    key = _key(chat_id)
    if pinned:
        _chat_language_pinned.add(key)
        _chat_language_streak.pop(key, None)
    elif key in _chat_language_pinned:
        return
    _chat_language[key] = language


def unpin_chat_language(chat_id: int | str | None) -> None:
    """Снять выбор языка кнопкой: дальше работает автоопределение."""
    key = _key(chat_id)
    _chat_language_pinned.discard(key)
    _chat_language_streak.pop(key, None)


def _language_evidence(text: str) -> tuple[str, float]:
    """Язык реплики и сила улик — с этим спорит выбор кнопкой.

    Очки берём у `faq_bot.lang.explain()`: он возвращает баллы обоих
    языков. Если сосед их не отдаёт, остаётся само решение и грубая
    оценка силы по числу слов — лишь бы не падать.
    """
    try:
        from . import lang as lang_module

        verdict = lang_module.DETECTOR.explain(text)
        margin = abs(float(verdict.ky_score) - float(verdict.ru_score))
        return str(verdict.language), margin
    except Exception:
        logger.debug("lang.explain() недоступен, беру грубую оценку улик")
    language = detect_language(text)
    strength = LANGUAGE_EVIDENCE_MARGIN if len(text.split()) >= 3 else 0.0
    return language, strength


def reply_language(chat_id: int | str | None, text: str) -> str:
    """Язык ответа на эту реплику.

    Без выбора кнопкой всё как раньше: язык определяется по тексту и
    запоминается за чатом.

    С выбором кнопкой правило такое.

    1. Реплика без явных улик другого языка («баланс», «2», «ооба»)
       отвечается на выбранном языке. Именно это и было сломано: кнопка
       показывала меню, а следующее сообщение снова уходило в
       автоопределение.
    2. Реплика с явными уликами другого языка (перевес не меньше
       :data:`LANGUAGE_EVIDENCE_MARGIN`) отвечается на языке реплики.
       Отвечать на кыргызском человеку, который по-русски спросил «как
       проверить баланс», — ошибка хуже, чем не заметить кнопку: кнопка
       задаёт язык интерфейса, а не переводчик.
    3. Если такие реплики идут :data:`LANGUAGE_RELEASE_STREAK` раз
       подряд, выбор кнопкой снимается: человек действительно перешёл на
       другой язык. Одна реплика выбор не отменяет — это может быть
       цитата, копипаста или название тарифа.
    """
    key = _key(chat_id)
    if not text.strip():
        return chat_language(chat_id)

    if key not in _chat_language_pinned:
        language = detect_language(text)
        set_chat_language(chat_id, language)
        return language

    chosen = chat_language(chat_id)
    language, strength = _language_evidence(text)
    if language == chosen or strength < LANGUAGE_EVIDENCE_MARGIN:
        _chat_language_streak.pop(key, None)
        return chosen

    streak = _chat_language_streak.get(key, 0) + 1
    _chat_language_streak[key] = streak
    if streak >= LANGUAGE_RELEASE_STREAK:
        logger.info("чат %s перешёл на %s, снимаю выбор кнопкой",
                    chat_id, language)
        unpin_chat_language(chat_id)
        set_chat_language(chat_id, language)
    return language


def forget_chat(chat_id: int | str | None) -> None:
    """Забыть всё про чат: тему диалога, отказы и выбранный язык."""
    key = _key(chat_id)
    _chat_language.pop(key, None)
    _chat_language_pinned.discard(key)
    _chat_language_streak.pop(key, None)
    throttle().forget(chat_id)
    conversation().forget(chat_id)
    reset_context(chat_id)


# --------------------------------------------------------------------------
# Защита от флуда
# --------------------------------------------------------------------------

class ThrottleGuard:
    """Не больше `limit` ответов в окно `window` секунд на один чат.

    Поводом стал живой тест: вставленный текст Telegram разрезал на семь
    сообщений, и бот выдал семь карточек подряд. Теперь лишние реплики
    получают одно короткое предупреждение, дальше — тишина до конца
    окна. Когда окно уезжает, чат снова отвечает: это ограничитель,
    а не бан.

    Пороги по умолчанию берутся из `bot_rules.json`; явные значения
    нужны тестам вместе с подменяемыми часами.
    """

    def __init__(self, limit: int | None = None, window: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sent: dict[str, list[float]] = {}
        self._warned: set[str] = set()

    @property
    def limit(self) -> int:
        """Сколько ответов разрешено в окно."""
        if self._limit is not None:
            return max(1, int(self._limit))
        return max(1, presentation.rules().limits.replies_per_window)

    @property
    def window(self) -> float:
        """Длина окна в секундах."""
        if self._window is not None:
            return float(self._window)
        return float(presentation.rules().limits.window_seconds)

    def check(self, chat_id: int | str | None) -> str:
        """Решение по реплике: «ok», «warn» (одно предупреждение) или «mute».

        Предупреждение само в лимит не попадает: иначе чат не смог бы
        получить его в следующем окне.
        """
        key = _key(chat_id)
        now = self._clock()
        window = self.window
        recent = [at for at in self._sent.get(key, ()) if now - at < window]
        if len(recent) < self.limit:
            recent.append(now)
            self._sent[key] = recent
            self._warned.discard(key)
            return "ok"
        self._sent[key] = recent
        if key in self._warned:
            return "mute"
        self._warned.add(key)
        return "warn"

    def forget(self, chat_id: int | str | None) -> None:
        """Забыть счётчики одного чата."""
        key = _key(chat_id)
        self._sent.pop(key, None)
        self._warned.discard(key)

    def clear(self) -> None:
        """Забыть счётчики всех чатов (тесты, перезапуск)."""
        self._sent.clear()
        self._warned.clear()


_throttle: ThrottleGuard | None = None


def throttle() -> ThrottleGuard:
    """Ограничитель ответов, общий для процесса."""
    global _throttle
    if _throttle is None:
        _throttle = ThrottleGuard()
    return _throttle


# --------------------------------------------------------------------------
# Ход разговора: дозревание ответа и отказ от ответа
# --------------------------------------------------------------------------

@dataclass
class Pending:
    """Последний ответ бота в чате — тот, который ещё можно переписать."""

    handle: Any                 # сообщение Telegram (всё, что умеет edit_text)
    parts: list[str]            # реплики, из которых собран вопрос
    at: float                   # когда ответ ушёл в чат
    revisions: int = 0


@dataclass(frozen=True)
class Served:
    """Тема, которую бот только что показал в этом чате."""

    faq_id: int
    at: float
    source: str                 # "text" — по вопросу, "button" — по кнопке


@dataclass(frozen=True)
class Rejected:
    """Темы, про которые в этом чате сказали «не помогло»."""

    topics: tuple[int, ...]
    count: int
    at: float


class ConversationFlow:
    """Состояние живого разговора: дозревание ответа и отказ от ответа.

    Две задачи, обе из живого теста на кыргызском.

    **Ответ дозревает, а не ждёт.** Раньше бот ждал две секунды тишины и
    только потом отвечал. Абонент печатал кыргызский текст с телефона,
    пауза внутри одной мысли оказалась больше двух секунд, и бот
    вклинился на середине. Просто увеличить окно нельзя: это задержало
    бы каждый одиночный вопрос на столько же. Поэтому первая реплика
    получает ответ **сразу**, а реплика, дописанная в окно
    :attr:`revise_window`, не присылает второе сообщение, а
    **переписывает** прежний ответ, посчитанный заново по склеенному
    тексту. Задержки нет, чат не растёт, ответ уточняется на лету — и
    это тот же принцип «редактировать, а не слать», на котором уже
    держатся кнопки.

    **Отказ от ответа.** Абонент говорит «баары бир иштебей жатат» —
    предыдущий совет не сработал. Повторять его дословно нельзя, и для
    этого нужно помнить, какую тему бот уже показывал (:meth:`serve`) и
    какие темы абонент уже отверг (:meth:`reject`).

    Где граница между «дописал свою мысль» и «прочитал ответ и говорит,
    что он не помог». Признака прочтения у Telegram Bot API нет вовсе,
    поэтому улик две:

    1. **нажатая кнопка** — абонент точно видел ответ, разговор пошёл
       дальше, переписывать больше нечего (:meth:`close`);
    2. **время** — :attr:`read_seconds` на то, чтобы прочитать. Слова
       отказа, пришедшие раньше, это продолжение своего же вопроса
       (абонент печатал, когда ответ появился); пришедшие позже —
       реакция на прочитанный ответ.

    Состояние живёт в памяти процесса и ничего не знает про Telegram:
    сообщение бота хранится как непрозрачный `handle`, поэтому класс
    тестируется обычными тестами.
    """

    def __init__(self, revise_window: float | None = None,
                 read_seconds: float | None = None,
                 max_revisions: int | None = None,
                 repeat_window: float | None = None,
                 escalate_after: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._revise_window = revise_window
        self._read_seconds = read_seconds
        self._max_revisions = max_revisions
        self._repeat_window = repeat_window
        self._escalate_after = escalate_after
        self._clock = clock
        self._pending: dict[str, Pending] = {}
        self._served: dict[str, Served] = {}
        self._rejected: dict[str, Rejected] = {}

    # --- пороги: явные значения для тестов, иначе из bot_rules.json ------

    @property
    def revise_window(self) -> float:
        """Окно дозревания ответа в секундах."""
        if self._revise_window is not None:
            return float(self._revise_window)
        return float(presentation.rules().limits.revise_window_seconds)

    @property
    def read_seconds(self) -> float:
        """Сколько нужно, чтобы прочитать ответ."""
        if self._read_seconds is not None:
            return float(self._read_seconds)
        return float(presentation.rules().limits.read_seconds)

    @property
    def max_revisions(self) -> int:
        """Сколько раз один ответ можно переписать."""
        if self._max_revisions is not None:
            return max(1, int(self._max_revisions))
        return max(1, presentation.rules().limits.max_revisions)

    @property
    def repeat_window(self) -> float:
        """Окно, в котором повтор темы сам по себе означает отказ."""
        if self._repeat_window is not None:
            return float(self._repeat_window)
        return float(presentation.rules().refusal.repeat_window_seconds)

    @property
    def escalate_after(self) -> int:
        """После какого числа отказов по теме звать человека."""
        if self._escalate_after is not None:
            return max(1, int(self._escalate_after))
        return max(1, presentation.rules().refusal.escalate_after)

    # --- дозревание ответа ----------------------------------------------

    @staticmethod
    def join(parts: Sequence[str]) -> str:
        """Склеить реплики в один вопрос."""
        return " ".join(part.strip() for part in parts if part and part.strip())

    def classify(self, chat_id: int | str | None, text: str) -> str:
        """«revision» — дописка к прежнему вопросу, «new» — новая реплика.

        Дописка переписывает прежний ответ, новая реплика получает новое
        сообщение. Правило целиком описано у класса.
        """
        waiting = self._pending.get(_key(chat_id))
        if waiting is None or waiting.handle is None:
            return "new"
        age = self._clock() - waiting.at
        if age > self.revise_window:
            return "new"
        if waiting.revisions >= self.max_revisions:
            logger.info("чат %s: ответ переписан %d раз, дальше новый",
                        chat_id, waiting.revisions)
            return "new"
        if age >= self.read_seconds and presentation.says_refusal(text):
            # Ответ успели прочитать, и абонент говорит, что он не помог.
            # Это реакция на ответ, а не продолжение вопроса.
            return "new"
        return "revision"

    def extend(self, chat_id: int | str | None, text: str) -> str:
        """Зачесть дописку и вернуть склеенный вопрос целиком.

        Окно отсчитывается заново от этой правки, а не от первого
        ответа: переписанный ответ абонент тоже ещё не читал, он в этот
        момент печатал. Иначе мысль из четырёх частей по четыре секунды
        всё равно разрывалась бы на середине — тот же дефект, только
        растянутый. Бесконечной правку это не делает: число правок
        ограничено :attr:`max_revisions`.
        """
        waiting = self._pending.get(_key(chat_id))
        if waiting is None:
            return (text or "").strip()
        waiting.parts.append(text or "")
        waiting.revisions += 1
        waiting.at = self._clock()
        return self.join(waiting.parts)

    def handle(self, chat_id: int | str | None) -> Any | None:
        """Сообщение бота, которое сейчас можно переписать."""
        waiting = self._pending.get(_key(chat_id))
        return waiting.handle if waiting is not None else None

    def opened(self, chat_id: int | str | None, text: str,
               handle: Any) -> None:
        """Запомнить отправленный ответ: он открыт для дозревания."""
        if handle is None:
            self.close(chat_id)
            return
        self._pending[_key(chat_id)] = Pending(handle=handle,
                                               parts=[text or ""],
                                               at=self._clock())

    def rebind(self, chat_id: int | str | None, handle: Any) -> None:
        """Переписать не удалось: дальше дозревает уже это сообщение."""
        key = _key(chat_id)
        waiting = self._pending.get(key)
        if waiting is None or handle is None:
            self.opened(chat_id, "", handle)
            return
        waiting.handle = handle
        waiting.at = self._clock()

    def close(self, chat_id: int | str | None) -> None:
        """Ответ прочитан или перекрыт: переписывать его больше нельзя."""
        self._pending.pop(_key(chat_id), None)

    def revisions(self, chat_id: int | str | None) -> int:
        """Сколько раз текущий ответ переписан (тесты и отладка)."""
        waiting = self._pending.get(_key(chat_id))
        return waiting.revisions if waiting is not None else 0

    # --- отказ от ответа -------------------------------------------------

    def serve(self, chat_id: int | str | None, faq_id: int | None,
              source: str = "text") -> None:
        """Запомнить тему, показанную в чате. `None` обрывает цепочку."""
        key = _key(chat_id)
        if faq_id is None:
            self._served.pop(key, None)
            return
        self._served[key] = Served(faq_id=int(faq_id), at=self._clock(),
                                   source=source)

    def served(self, chat_id: int | str | None) -> Served | None:
        """Тема, показанная последней, пока она не устарела."""
        key = _key(chat_id)
        shown = self._served.get(key)
        if shown is None:
            return None
        if self._clock() - shown.at > self.repeat_window:
            self._served.pop(key, None)
            return None
        return shown

    def reject(self, chat_id: int | str | None, faq_id: int | None) -> int:
        """Запомнить, что тема не помогла. Возвращает номер отказа."""
        key = _key(chat_id)
        now = self._clock()
        previous = self._rejected.get(key)
        fresh = previous is not None and now - previous.at <= self.repeat_window
        topics = previous.topics if fresh and previous is not None else ()
        count = previous.count if fresh and previous is not None else 0
        if faq_id is not None and int(faq_id) not in topics:
            topics = topics + (int(faq_id),)
        self._rejected[key] = Rejected(topics=topics, count=count + 1, at=now)
        self._served.pop(key, None)
        return count + 1

    def rejected(self, chat_id: int | str | None) -> tuple[int, ...]:
        """Темы, про которые в этом чате сказали «не помогло»."""
        log = self._rejection_log(chat_id)
        return log.topics if log is not None else ()

    def refusals(self, chat_id: int | str | None) -> int:
        """Сколько отказов подряд в этом чате."""
        log = self._rejection_log(chat_id)
        return log.count if log is not None else 0

    def _rejection_log(self, chat_id: int | str | None) -> Rejected | None:
        """Живой журнал отказов чата.

        Журнал живёт то же окно, что и признак повтора: через три минуты
        без отказов разговор считается новым, и ранее отвергнутая тема
        снова имеет право на ответ. Иначе абонент, которому утром не
        помог совет про интернет, вечером вообще не получил бы ответа
        про интернет.
        """
        key = _key(chat_id)
        log = self._rejected.get(key)
        if log is None:
            return None
        if self._clock() - log.at > self.repeat_window:
            self._rejected.pop(key, None)
            return None
        return log

    def satisfied(self, chat_id: int | str | None) -> None:
        """Вопрос закрыт («Понятно», оператор): цепочка отказов забыта."""
        key = _key(chat_id)
        self._rejected.pop(key, None)
        self._served.pop(key, None)

    # --- уборка ----------------------------------------------------------

    def forget(self, chat_id: int | str | None) -> None:
        """Забыть разговор одного чата."""
        key = _key(chat_id)
        self._pending.pop(key, None)
        self._served.pop(key, None)
        self._rejected.pop(key, None)

    def clear(self) -> None:
        """Забыть разговоры всех чатов (тесты, перезапуск)."""
        self._pending.clear()
        self._served.clear()
        self._rejected.clear()


_flow: ConversationFlow | None = None


def conversation() -> ConversationFlow:
    """Состояние разговоров, общее для процесса."""
    global _flow
    if _flow is None:
        _flow = ConversationFlow()
    return _flow


def reset_runtime_state() -> None:
    """Забыть всё состояние слоя бота: язык, троттлинг, разговоры, тему.

    Ограничитель и состояние разговоров пересоздаются, а не просто
    чистятся: тест мог подменить их своими порогами и часами, и это не
    должно протекать в следующий тест.
    """
    global _throttle, _flow
    _chat_language.clear()
    _chat_language_pinned.clear()
    _chat_language_streak.clear()
    if _throttle is not None:
        _throttle.clear()
        _throttle = None
    if _flow is not None:
        _flow.clear()
        _flow = None
    reset_context()


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------

def _operator_screen(chat_id: int | str | None,
                     reason: str | None = None,
                     language: str | None = None,
                     note: str | None = None) -> Screen:
    """Экран передачи оператору; причина уходит в агрегат /stats."""
    operator = presentation.rules().operator
    if language not in config.LANGUAGES:
        language = chat_language(chat_id)
    if reason:
        STATS.record_operator(reason, chat_id)
        MEMORY.forget_misses(chat_id)
    return presentation.operator_screen(entry_by_id(operator.faq_id),
                                        language, reason, note)


def screen_for_text(text: str,
                    chat_id: int | str | None = None,
                    revision: bool = False,
                    metered: bool = True) -> Screen | None:
    """Экран в ответ на свободный ввод или цифру меню.

    Порядок веток осознанный: сначала защита от флуда, потом цифра меню
    и просьба об операторе, потом приветствия и болтовня, и только затем
    `answer()`. Так «салам» получает приветствие, а «салам, интернет
    иштебейт» — настоящий поиск по базе.

    `revision=True` означает, что абонент дописал реплику к своему же
    вопросу и экран пойдёт правкой прежнего ответа. Тогда это не новая
    реплика, а уточнение прежней, поэтому:

    * бюджет ограничителя не расходуется — иначе человек, пишущий мысль
      пятью сообщениями, сам себя заглушил бы;
    * признаки отказа не проверяются — абонент ещё не читал ответ,
      он его дописывает;
    * прежняя запись в памяти диалога отменяется (`MEMORY.rollback`):
      одна мысль обязана остаться одной репликой.

    `metered=False` означает, что бюджет ограничителя за эту реплику уже
    израсходован вызывающим. Так приходит распознанная речь: голосовое
    проверяется ограничителем до распознавания, потому что распознавание
    платное, а второй раз считать ту же реплику нельзя.

    `None` означает намеренную тишину: чат превысил лимит ответов и уже
    получил предупреждение.
    """
    stripped = (text or "").strip()
    language = reply_language(chat_id, stripped)
    if not stripped:
        return presentation.menu_screen(language)

    if metered and not revision:
        verdict = throttle().check(chat_id)
        if verdict == "mute":
            logger.info("чат %s заглушен: больше %d ответов в окно",
                        chat_id, throttle().limit)
            return None
        if verdict == "warn":
            return presentation.notice_screen("too_fast", language,
                                              with_menu=False)

    limits = presentation.rules().limits
    if len(stripped) > limits.max_question_chars:
        logger.info("реплика чата %s длиной %d знаков не разбирается",
                    chat_id, len(stripped))
        return presentation.notice_screen("too_long", language)

    if stripped.isdigit() and len(stripped) == 1:
        section = presentation.section_by_number(int(stripped))
        if section is not None:
            if section.number == 0:
                return _operator_screen(chat_id, "direct_request", language)
            return presentation.section_screen(section, all_entries(),
                                               language)

    reason = presentation.operator_reason(stripped)
    if reason == "direct_request":
        return _operator_screen(chat_id, reason, language)

    intent = presentation.detect_intent(stripped)
    if intent is not None:
        return presentation.smalltalk_screen(intent, language)

    if revision:
        MEMORY.rollback(chat_id)
        # Та же причина, что и у памяти диалога: мысль, дописанная тремя
        # репликами, — это ОДИН вопрос абонента. Без отмены /stats считал
        # бы три обращения вместо одного, а первая незаконченная часть
        # («крч проблема такая») попадала бы в примеры неотвеченных —
        # хотя ответ в итоге был найден и показан.
        STATS.rollback()

    reply = respond(stripped, chat_id=chat_id, language=language)
    set_chat_language(chat_id, reply.lang)
    screen = presentation.answer_card(reply)

    if reason is not None:
        # Жалоба, возврат, кража. Если тема в базе всё-таки нашлась
        # («украли телефон, блокируйте сим» — это тема 13), абонент
        # получает инструкцию из faq.json И передачу человеку. Инструкция
        # вместо человека была бы формальной отпиской, человек вместо
        # инструкции — потерей готового ответа.
        if reply.faq_id is None:
            return _operator_screen(chat_id, reason, reply.lang)
        STATS.record_operator(reason, chat_id)
        conversation().serve(chat_id, reply.faq_id)
        return presentation.with_operator_offer(screen, reply.lang, reason,
                                                "escalated")

    if not revision:
        refused = _refusal(stripped, reply, chat_id)
        if refused is not None:
            return _refusal_screen(refused, reply, chat_id, reply.lang)

    if reply.faq_id is None:
        screen = _maybe_offer_operator(screen, chat_id, reply.lang)
    conversation().serve(chat_id, reply.faq_id)
    return screen


def _maybe_offer_operator(screen: Screen, chat_id: int | str | None,
                          language: str) -> Screen:
    """После нескольких отказов подряд предложить оператора самому."""
    threshold = presentation.rules().operator.repeated_miss_threshold
    if MEMORY.misses(chat_id) < threshold:
        return screen
    MEMORY.forget_misses(chat_id)
    STATS.record_operator("repeated_miss", chat_id)
    logger.info("чат %s не понят %d раза подряд, предлагаю оператора",
                chat_id, threshold)
    return presentation.with_operator_offer(screen, language, "repeated_miss")


# --------------------------------------------------------------------------
# «Этот совет не помог»
# --------------------------------------------------------------------------

def _refusal(text: str, reply: Reply,
             chat_id: int | str | None) -> tuple[str, int] | None:
    """Признак отказа от предыдущего ответа и тема, которая не помогла.

    Три признака, от бесспорного к осторожному.

    * `rejected` — поиск снова вывел на тему, про которую в этом
      разговоре уже сказали «не помогло». Второй раз подсовывать
      заведомо бесполезный совет нельзя, что бы абонент ни написал.
    * `repeat` — та же тема выдаётся текстовым вопросом второй раз
      подряд внутри окна повтора. Это и был дефект живого теста: бот
      дословно повторил карточку. Два исключения. Нажатая кнопка в
      признак не считается: там тему выбрал сам абонент, и повторять его
      собственный выбор — не ошибка бота. Унаследованная из контекста
      тема — тоже: «а сколько стоит?» после вопроса о роуминге это
      уточнение по той же теме, и ответить на него обязан пункт 4 ТЗ,
      а не экран «совет не помог».
    * `said` — абонент сказал «не помогло», «баары бир». Слов мало:
      требуется, чтобы новый поиск не нашёл другую тему. «Не помогло,
      а как пополнить счёт?» — это новый вопрос, и отвечать надо на
      него, а не на прежнюю тему.
    """
    flow = conversation()
    rejected = flow.rejected(chat_id)
    if reply.faq_id is not None and reply.faq_id in rejected:
        return "rejected", int(reply.faq_id)

    shown = flow.served(chat_id)
    if (shown is not None and shown.source == "text"
            and reply.faq_id == shown.faq_id
            and not reply.inherited_from_context):
        return "repeat", shown.faq_id

    if not presentation.says_refusal(text):
        return None
    # От какой темы отказываются. Обычно это последняя показанная; если
    # показанной нет, значит цепочка отказов уже идёт («не помогло» —
    # подсказки — «всё равно не работает»), и речь о последней
    # отвергнутой теме.
    topic = shown.faq_id if shown is not None else (
        rejected[-1] if rejected else None)
    if topic is not None and reply.faq_id in (None, topic):
        return "said", topic
    return None


def _alternatives(text: str, reply: Reply, chat_id: int | str | None,
                  rejected: int) -> list[FaqEntry]:
    """До трёх тем, которых абонент в этом разговоре ещё не отвергал.

    Сначала идут соседи по разделу меню: «почему не работает интернет»
    стоит рядом с «настройкой APN» и «остатком пакетов», и именно это
    следующий вопрос живого оператора. Разделы собраны руками, поэтому
    подсказка получается осмысленной даже тогда, когда в самой реплике
    нет ни одного слова по делу.

    Кандидаты поиска добавляются только если абонент назвал тему своими
    словами. На голом «не помогло» поиск возвращает шум — «как узнать
    свой номер» вместо настройки APN, — и показывать его стыдно.
    """
    skip = set(conversation().rejected(chat_id)) | {rejected}
    order: list[int] = []
    section = presentation.section_for_faq(rejected)
    if section is not None:
        order += [item for item in section.faq_ids if item not in skip]
    if mentions_topic(text):
        order += [item for item in reply.suggestions
                  if item not in skip and item not in order]
    found = (entry_by_id(item) for item in order[:config.TOP_K])
    return [entry for entry in found if entry is not None]


def _refusal_screen(refused: tuple[str, int], reply: Reply,
                    chat_id: int | str | None, language: str) -> Screen:
    """Ответ на «не помогло»: другие темы, а при повторе — человек.

    Тот же текст не повторяется никогда. На первый отказ абонент видит
    до трёх других тем кнопками и предложение оператора, на следующий —
    сразу оператора с причиной `not_helping`: советы по этой теме
    кончились, и честнее сказать это, чем перебирать базу дальше.
    """
    signal, topic = refused
    flow = conversation()
    count = flow.reject(chat_id, topic)
    logger.info("чат %s: отказ от темы %s (признак %s), отказ номер %d",
                chat_id, topic, signal, count)

    if count >= flow.escalate_after:
        flow.satisfied(chat_id)
        return _operator_screen(chat_id, "not_helping", language,
                                note="no_more_advice")

    return presentation.alternatives_screen(
        _alternatives(reply.text, reply, chat_id, topic), language,
        entry_by_id(topic))


def screen_for_action(action: str, chat_id: int | str | None = None) -> Screen:
    """Экран в ответ на нажатие кнопки.

    Нажатие `faq:<id>` обязано выдать ответ именно по этой теме — это
    пункт 3 обязательной части.

    Экран подменяет собой сообщение, в котором стоит кнопка: раздел меню
    превращается в список тем, тема — в карточку ответа, «Понятно» — в
    одну строку подтверждения. Новых сообщений при этом не появляется,
    этим занимается :func:`_on_button`.

    Отдельный случай — `lang:<ru|ky>:<адрес экрана>`. Язык запоминается
    за чатом, после чего тот же экран собирается заново на новом языке:
    абонент видит тот же ответ по-кыргызски, а не меню вместо ответа.
    """
    command, _, argument = (action or "").partition(":")
    language = chat_language(chat_id)

    if command == "faq" and argument.isdigit():
        reply = reply_for_faq_id(int(argument), language, chat_id=chat_id)
        if reply is not None:
            # Тему выбрал сам абонент. Запоминаем её, чтобы потом понять
            # «баары бир иштебей жатат», но помечаем источником кнопку:
            # повтор собственного выбора отказом не считается.
            conversation().serve(chat_id, reply.faq_id, "button")
            return presentation.answer_card(reply)
        return presentation.menu_screen(language)

    if command == "section" and argument.isdigit():
        section = presentation.section_by_number(int(argument))
        if section is None:
            return presentation.menu_screen(language)
        if section.number == 0:
            return _operator_screen(chat_id, "direct_request", language)
        return presentation.section_screen(section, all_entries(), language)

    if command == "operator":
        conversation().satisfied(chat_id)
        return _operator_screen(chat_id, "direct_request", language)
    if command == "ack":
        # «Понятно» — это «совет подошёл». Цепочка отказов закрыта, и тот
        # же вопрос позже снова получит обычный ответ, а не подсказки.
        conversation().satisfied(chat_id)
        reply = _reply_for_argument(argument, language, chat_id)
        if reply is None:
            return presentation.acknowledged_screen(language)
        return presentation.acknowledged_card(reply)
    if command == "another":
        reset_context(chat_id)
        conversation().forget(chat_id)
        reply = _reply_for_argument(argument, language, chat_id)
        if reply is None:
            return presentation.another_question_screen(language)
        return presentation.another_question_card(reply)
    if command == "help":
        return presentation.help_screen(language)
    if command == "lang":
        return _language_screen(argument, chat_id, language)
    if command == "stats":
        return stats_screen(chat_id)
    return presentation.menu_screen(language)


def _reply_for_argument(argument: str, language: str,
                        chat_id: int | str | None) -> Reply | None:
    """Ответ по номеру темы из адреса кнопки (`ack:4`, `another:4`).

    `None` означает, что номера в кнопке нет или темы с таким номером не
    существует: кнопка пришла из старого сообщения, и пересобирать
    карточку не из чего.

    Контекст диалога здесь не трогаем намеренно: «Понятно» и «Другой
    вопрос» — это не новый вопрос по теме, а закрытие прежнего.
    """
    if not argument.isdigit():
        return None
    entry = entry_by_id(int(argument))
    if entry is None:
        return None
    return Reply(
        text=entry.question(language),
        lang=language,
        faq_id=entry.id,
        confidence=1.0,
        candidates=(entry.id,),
        suggestions=(entry.id,),
        entry=entry,
        suggestion_entries=(entry,),
    )


def _language_screen(argument: str, chat_id: int | str | None,
                     language: str) -> Screen:
    """Переключить язык чата и перерисовать тот же экран на новом языке.

    `argument` — это `<код языка>` или `<код языка>:<адрес экрана>`,
    например `ky:faq:4`. Адрес кладёт в кнопку сама `presentation`, так
    что состояние хранится в самой кнопке, а не в памяти процесса.

    Повторный `lang:` в адресе игнорируется: он означал бы бесконечную
    перерисовку, а битая кнопка не имеет права ронять бота.
    """
    chosen, _, target = argument.partition(":")
    if chosen not in config.LANGUAGES:
        return presentation.menu_screen(language)
    set_chat_language(chat_id, chosen, pinned=True)
    if target and not target.startswith("lang"):
        return screen_for_action(target, chat_id)
    return presentation.language_switched_screen(chosen)


def stats_screen(chat_id: int | str | None = None) -> Screen:
    """Экран /stats: агрегат неотвеченных вопросов без персональных данных."""
    return presentation.stats_screen(stats_snapshot(), all_entries(),
                                     chat_language(chat_id))


# --------------------------------------------------------------------------
# Telegram: всё ниже требует библиотеку и импортирует её лениво
# --------------------------------------------------------------------------

def _markup(screen: Screen) -> Any | None:
    """Описание кнопок -> InlineKeyboardMarkup. Импорт только здесь."""
    if not screen.rows:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = [[InlineKeyboardButton(button.label, callback_data=button.action)
                 for button in row] for row in screen.rows]
    return InlineKeyboardMarkup(keyboard)


async def _send(message: Any, screen: Screen) -> Any:
    """Отправить экран новым сообщением и вернуть отправленное.

    Новое сообщение появляется только в ответ на первую реплику мысли и
    на команду. Нажатие кнопки сообщений не плодит (:func:`_edit`),
    дописанная реплика — тоже (:func:`_rewrite`): она переписывает то
    сообщение, которое вернула эта функция.
    """
    return await message.reply_text(
        screen.text,
        parse_mode=screen.parse_mode,
        reply_markup=_markup(screen),
        disable_web_page_preview=True,
    )


#: Так Telegram отвечает на попытку переписать сообщение тем же текстом
#: и той же клавиатурой. Это не ошибка: абонент просто нажал кнопку
#: экрана, на котором уже стоит.
NOT_MODIFIED = "message is not modified"


def _is_not_modified(error: BaseException) -> bool:
    """Жалоба Telegram «текст не изменился»?"""
    return NOT_MODIFIED in str(error).lower()


async def _apply(call: Callable[..., Any], screen: Screen) -> str:
    """Переписать сообщение вызовом `call`.

    Возвращает «edited», «unchanged» или «failed» — состояние нужно
    тестам и логу. Новых сообщений функция не отправляет: именно от них
    чат превращался в простыню, где не найти сам ответ.

    `reply_markup=None` заодно убирает клавиатуру — так карточка
    сворачивается в одну строку после «Понятно».
    """
    try:
        from telegram.error import TelegramError as _Error
    except ImportError:                       # pragma: no cover
        _Error = Exception                    # type: ignore[assignment, misc]

    try:
        await call(
            screen.text,
            parse_mode=screen.parse_mode,
            reply_markup=_markup(screen),
            disable_web_page_preview=True,
        )
    except _Error as error:
        if _is_not_modified(error):
            logger.debug("экран не изменился, Telegram правку отклонил")
            return "unchanged"
        logger.warning("не удалось переписать сообщение: %s", error)
        return "failed"
    return "edited"


async def _edit(query: Any, screen: Screen) -> str:
    """Переписать сообщение, в котором нажата кнопка."""
    return await _apply(query.edit_message_text, screen)


async def _rewrite(sent: Any, screen: Screen) -> str:
    """Переписать свой же прежний ответ: абонент дописал реплику.

    Правка, а не новое сообщение: ответ уточняется на лету, задержки
    нет, переписка не растёт. Если сообщение переписать нельзя (его
    удалили, оно слишком старое), возвращается «failed», и вызывающий
    отвечает новым сообщением — молчание было бы хуже.
    """
    if sent is None:
        return "failed"
    editor = getattr(sent, "edit_text", None)
    if editor is None:                        # pragma: no cover
        logger.warning("сообщение не умеет edit_text, отвечаю новым")
        return "failed"
    return await _apply(editor, screen)


async def _on_start(update: Any, context: Any) -> None:
    """/start — USSD-меню. Экрана выбора языка нет намеренно."""
    chat_id = update.effective_chat.id
    conversation().close(chat_id)
    await _send(update.effective_message,
                presentation.start_screen(chat_language(chat_id)))


async def _on_help(update: Any, context: Any) -> None:
    """/help — короткая справка."""
    chat_id = update.effective_chat.id
    conversation().close(chat_id)
    await _send(update.effective_message,
                presentation.help_screen(chat_language(chat_id)))


async def _on_stats(update: Any, context: Any) -> None:
    """/stats — агрегат неотвеченных вопросов."""
    chat_id = update.effective_chat.id
    conversation().close(chat_id)
    await _send(update.effective_message, stats_screen(chat_id))


async def _on_reset(update: Any, context: Any) -> None:
    """/reset — забыть тему диалога и выбранный язык."""
    chat_id = update.effective_chat.id
    forget_chat(chat_id)
    await _send(update.effective_message,
                presentation.another_question_screen(chat_language(chat_id)))


async def _on_text(update: Any, context: Any) -> None:
    """Свободный вопрос абонента: ответ сразу, уточнение — правкой ответа.

    Первая реплика получает ответ немедленно. Дописанная в окно
    дозревания реплика нового сообщения не приносит: бот считает ответ
    заново по склеенному тексту и переписывает им свой же прежний ответ.
    Правило «дописка или реакция» целиком живёт в
    :class:`ConversationFlow`, здесь только отправка и правка.
    """
    message = update.effective_message
    chat_id = update.effective_chat.id
    text = message.text or ""
    flow = conversation()

    if flow.classify(chat_id, text) == "revision":
        joined = flow.extend(chat_id, text)
        screen = screen_for_text(joined, chat_id, revision=True)
        if screen is None:                  # троттлинг: намеренная тишина
            return
        screen = presentation.with_revision_note(screen,
                                                 chat_language(chat_id))
        if await _rewrite(flow.handle(chat_id), screen) != "failed":
            return
        # Переписать нечего (сообщение удалили). Молчать нельзя: отвечаем
        # новым сообщением по тому же склеенному тексту.
        flow.rebind(chat_id, await _send(message, screen))
        return

    screen = screen_for_text(text, chat_id)
    if screen is None:
        flow.close(chat_id)
        return
    flow.opened(chat_id, text, await _send(message, screen))


#: Какое уведомление показать на каждый неудачный статус распознавания.
#: Коды совпадают со статусами `faq_bot.speech`, но перечислены строками:
#: модуль распознавания импортируется лениво и не нужен ни `answer()`, ни
#: тестам без голоса. `failed` и `nothing` ведут к одному тексту — абоненту
#: в обоих случаях нужно одно и то же: повторить или написать.
VOICE_NOTICES: Mapping[str, str] = {
    "too_long": "voice_too_long",
    "rate_limited": "voice_rate_limited",
    "no_key": "voice_no_key",
    "nothing": "voice_nothing",
    "failed": "voice_nothing",
}


async def _deliver(message: Any, handle: Any, screen: Screen) -> None:
    """Показать экран правкой своего сообщения, а если нельзя — новым."""
    if await _rewrite(handle, screen) == "failed":
        await _send(message, screen)


async def _transcribe(voice: Any, chat_id: int | str | None) -> Any:
    """Скачать запись, распознать и удалить файл. Исключений не бросает.

    Распознавание синхронное (urllib с таймаутом до минуты) и уходит в
    отдельный поток: иначе одно голосовое заморозило бы весь бот,
    включая текстовые вопросы остальных абонентов.

    Временный каталог удаляется в `finally` при любом исходе: голос
    абонента не имеет права остаться на диске сервера.
    """
    # Ленивый импорт: распознавание нужно только боту и только здесь.
    from . import speech

    folder = tempfile.mkdtemp(prefix="faq_voice_")
    try:
        path = Path(folder) / "voice.oga"
        source = await voice.get_file()
        await source.download_to_drive(custom_path=str(path))
        seconds = float(getattr(voice, "duration", 0) or 0)
        return await asyncio.to_thread(speech.transcribe, path,
                                       chat_id=str(chat_id),
                                       declared_seconds=seconds)
    except Exception:
        logger.exception("не удалось получить голосовое сообщение")
        return speech.Transcript(speech.FAILED)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def _on_voice(update: Any, context: Any) -> None:
    """Голосовое сообщение: распознаём и отвечаем обычным путём.

    Своей логики ответа у голоса нет: распознанный текст идёт в
    :func:`screen_for_text`, как набранный руками. Отличий ровно три.

    1. **Эхо.** Перед карточкой печатается то, что бот услышал:
       распознавание ошибается, и без эха абонент не поймёт, почему
       ответ мимо.
    2. **Ограничитель раньше распознавания.** Распознавание платное и
       считается по секундам записи, поэтому лишняя реплика отсекается
       до обращения к сети, а не после.
    3. **Без дозревания.** Голосовые не дописывают порциями, поэтому
       ответ на голосовое не открыт для правки следующей репликой.
       Открытое дозревание прежнего текстового ответа закрывается:
       голосовое — это новая реплика.

    Пределы длины и бюджеты живут в `faq_bot.speech` и здесь не
    ослабляются: ключ оплачивает человек.
    """
    message = update.effective_message
    chat_id = update.effective_chat.id
    language = chat_language(chat_id)
    conversation().close(chat_id)

    verdict = throttle().check(chat_id)
    if verdict == "mute":
        logger.info("чат %s заглушен, голосовое не распознаётся", chat_id)
        return
    if verdict == "warn":
        await _send(message, presentation.notice_screen("too_fast", language,
                                                        with_menu=False))
        return

    voice = getattr(message, "voice", None)
    if voice is None:                              # pragma: no cover
        return

    waiting = await _send(message, presentation.listening_screen(language))
    transcript = await _transcribe(voice, chat_id)
    if not transcript.ok:
        notice = VOICE_NOTICES.get(transcript.status, "voice_nothing")
        logger.info("чат %s: голосовое не распознано (%s)",
                    chat_id, transcript.status)
        await _deliver(message, waiting,
                       presentation.notice_screen(notice, language))
        return

    heard = transcript.text.strip()
    logger.info("чат %s: распознано %.1f с речи, %d знаков",
                chat_id, transcript.seconds, len(heard))
    screen = screen_for_text(heard, chat_id, metered=False)
    if screen is None:                             # pragma: no cover
        return
    await _deliver(message, waiting,
                   presentation.with_heard(screen, heard,
                                           chat_language(chat_id)))


async def _on_button(update: Any, context: Any) -> None:
    """Нажатие кнопки переписывает то сообщение, в котором эта кнопка.

    Раздел меню превращается в список тем, тема — в карточку ответа,
    «Меню» — обратно в меню. Новых сообщений не отправляется ни одного:
    за минуту живого теста три сообщения на касание превращали чат в
    двадцать сообщений, среди которых не найти ответ.

    Нажатие закрывает окно дозревания ответа: раз абонент нажал кнопку,
    он ответ видел, и дописывать к прежнему вопросу уже нечего.

    `answer_callback_query` вызывается первым и всегда: без него у
    кнопки до сорока секунд крутятся часики, даже если экран уже
    перерисован.
    """
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    conversation().close(chat_id)
    screen = screen_for_action(query.data or "", chat_id)
    await _edit(query, screen)


def build_application(token: str) -> Any:
    """Собрать приложение Telegram. Библиотека импортируется здесь."""
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", _on_start))
    application.add_handler(CommandHandler("help", _on_help))
    application.add_handler(CommandHandler("stats", _on_stats))
    application.add_handler(CommandHandler("reset", _on_reset))
    application.add_handler(CallbackQueryHandler(_on_button))
    application.add_handler(MessageHandler(filters.VOICE, _on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           _on_text))
    application.add_error_handler(_on_error)
    return application


CONFLICT_HINT = (
    "\nЗапущен ещё один экземпляр бота — Telegram отдаёт очередь сообщений\n"
    "только одному. Остановите лишние:\n"
    "    python tools/run_bot.py --stop\n"
    "и запустите заново:\n"
    "    python tools/run_bot.py\n"
)


async def _on_error(update: Any, context: Any) -> None:
    """Единая обработка сбоев: без трассировки в лицо и с внятной причиной.

    Без обработчика библиотека на каждую ошибку печатает «No error handlers
    are registered» и полный трейсбек. На защите это выглядит как падение,
    хотя чаще всего причина бытовая — второй запущенный экземпляр.
    """
    error = getattr(context, "error", None)

    # Конфликт очереди — самая частая и самая безобидная ошибка.
    # Она повторяется каждые несколько секунд, поэтому печатаем подсказку
    # один раз, дальше только короткая строка в лог.
    if type(error).__name__ == "Conflict":
        if not getattr(_on_error, "_conflict_reported", False):
            print(CONFLICT_HINT)
            _on_error._conflict_reported = True        # type: ignore[attr-defined]
        logger.warning("конфликт очереди: работает другой экземпляр бота")
        return

    if type(error).__name__ in ("NetworkError", "TimedOut"):
        logger.warning("сеть недоступна, попробуем ещё раз: %s", error)
        return

    logger.exception("необработанный сбой", exc_info=error)

    # Абонент не должен остаться без ответа из-за нашей ошибки.
    chat = getattr(getattr(update, "effective_chat", None), "id", None)
    if chat is None:
        return
    try:
        language = chat_language(chat)
        await context.bot.send_message(
            chat_id=chat,
            text=presentation.message("service_unavailable", language),
        )
    except Exception:                                   # noqa: BLE001
        logger.warning("не удалось сообщить абоненту о сбое")


def token_from_env_file(name: str = TOKEN_ENV) -> str:
    """Значение из файла `.env` в корне проекта.

    Переменная окружения остаётся главной, но держать её в сессии
    терминала неудобно: она теряется при каждом новом окне, и человек
    запускает бота без токена снова и снова. Файл `.env` исключён из
    git (см. `.gitignore`), поэтому секрет в репозиторий не попадает.
    """
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return ""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def run(token: str | None = None) -> int:
    """Запустить бота. Возвращает код выхода, исключений наружу не бросает."""
    token = (token or os.environ.get(TOKEN_ENV) or token_from_env_file()).strip()
    if not token:
        print(NO_TOKEN_MESSAGE)
        return 2

    try:
        application = build_application(token)
    except ImportError:
        print(NO_LIBRARY_MESSAGE)
        return 2

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    # httpx на уровне INFO печатает полный URL каждого запроса к Telegram,
    # а токен бота — это часть URL. В любом сохранённом логе он оказался бы
    # в открытом виде, и его пришлось бы отзывать. Поднимаем порог до
    # предупреждений: ошибки сети по-прежнему видны, токен — нет.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    rules = presentation.rules()
    logger.info("правила поведения: %s", rules.source)
    logger.info("ответ дозревает %.1f с (до %d правок), чтение %.1f с, "
                "не больше %d ответов в %.0f с, вопрос до %d знаков",
                rules.limits.revise_window_seconds,
                rules.limits.max_revisions,
                rules.limits.read_seconds,
                rules.limits.replies_per_window,
                rules.limits.window_seconds,
                rules.limits.max_question_chars)
    logger.info("повтор темы считается отказом в окне %.0f с, "
                "оператор после %d отказов",
                rules.refusal.repeat_window_seconds,
                rules.refusal.escalate_after)
    print("Бот запущен. Остановить — Ctrl+C.")
    try:
        application.run_polling()
    except KeyboardInterrupt:              # pragma: no cover
        print("\nОстановлен.")
    return 0
