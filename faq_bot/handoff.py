"""Передача обращения живому оператору — в том же Telegram.

Пока «передача оператору» была надписью на экране, пункт закрывался
формально: бот помечал обращение, но по ту сторону никого не было.
Здесь передача настоящая, без веб-панели и без своего сервера: оператор
сидит в обычном Telegram, в личном чате или в группе поддержки.

Как это работает.

1. Чат оператора задаётся переменной окружения `OPERATOR_CHAT_ID` —
   читается так же, как токен: из окружения, а если там пусто, то из
   файла `.env` в корне проекта. В репозиторий `.env` не попадает.
   **Если переменной нет, всё работает как раньше**: бот помечает
   обращение, карточку никуда не отправляет, ошибок не печатает.
2. При передаче бот присылает в этот чат карточку обращения
   (:func:`card`): номер обращения, причина передачи, язык абонента,
   текст вопроса, что успел ответить сам бот и время.
3. Оператор отвечает **реплаем** на карточку. Бот ловит реплай и
   доставляет текст абоненту с пометкой «🧑‍💼 Ответ оператора».
4. Связь «карточка в операторском чате ↔ чат абонента» живёт в памяти
   процесса (:class:`HandoffDesk`), по `message_id` карточки. Базы
   данных в задаче нет, поэтому после перезапуска бота старые карточки
   неизвестны — и реплай на такую карточку получает внятный ответ, а не
   молчание.

Какие данные уезжают в операторский чат и почему именно они.

* **номер обращения** — чтобы человеку было чем его называть
  («по обращению 0007»); номер последовательный, из него ничего не
  узнать об абоненте;
* **язык абонента** — оператор обязан ответить на языке вопроса, это
  требование ТЗ, и угадывать язык по тексту человек не должен;
* **текст вопроса** — предмет обращения, без него помогать нечему;
* **что ответил бот** (тема, её формулировка, уверенность) — чтобы
  оператор не начал с того же совета, который уже не помог;
* **время** — очередь и часы работы поддержки.

Чего в карточке нет намеренно: имени, фамилии, `username`, телефона и
самого `chat_id` абонента. Ответ доставляется механизмом реплая, поэтому
знать чат абонента оператору не нужно вовсе — связь держит бот. Имя и
`username` — персональные данные, которые для ответа по существу не
нужны: уточнения организаторов прямо требуют не раскрывать чужие
персональные данные, а «не собирать лишнего» — то же правило, только на
шаг раньше.

Telegram здесь не импортируется: модуль занимается памятью и текстом,
отправку делает `bot.py`. Поэтому передача оператору проверяется
обычными тестами, а контракт `answer()` остаётся без Telegram.
"""
from __future__ import annotations

import html
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config
from .presentation import Screen, message, reason_label

logger = logging.getLogger(__name__)

#: Переменная окружения с чатом оператора. Пусто — передача не пересылается.
OPERATOR_CHAT_ENV = "OPERATOR_CHAT_ID"

#: Тот же `.env`, из которого берётся токен бота. В git не попадает.
ENV_FILE: Path = Path(__file__).resolve().parents[1] / ".env"

#: Сколько карточек помним. Память процесса не резиновая, а старые
#: обращения оператор всё равно не комментирует.
MAX_TICKETS: int = 500

#: До какой длины везём вопрос в карточку. Длинные сообщения бот и так
#: не разбирает (см. `limits.max_question_chars`), это защита от остатка.
QUESTION_LENGTH: int = 400

#: Сколько секунд вопрос абонента считается свежим. Нужно для кнопки
#: «0 Оператор»: у нажатия своего текста нет, и в карточку идёт последний
#: вопрос этого чата — но только если он из того же разговора.
QUESTION_TTL: float = float(config.CONTEXT_TTL_SECONDS)


# --------------------------------------------------------------------------
# Настройка: переменная окружения или .env
# --------------------------------------------------------------------------

def from_env_file(name: str, path: Path | None = None) -> str:
    """Значение переменной из файла `.env` в корне проекта.

    Переменная окружения остаётся главной, но держать её в сессии
    терминала неудобно: она теряется при каждом новом окне. Файл `.env`
    исключён из git, поэтому секрет в репозиторий не попадает.

    Нечитаемый или испорченный файл — это пустая строка, а не падение:
    из-за конфигурации бот останавливаться не имеет права.
    """
    env_file = path or ENV_FILE
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def env_value(name: str, path: Path | None = None) -> str:
    """Переменная окружения, а если её нет — строка из `.env`."""
    return (os.environ.get(name) or from_env_file(name, path)).strip()


def operator_chat() -> str:
    """Чат оператора или пустая строка, если передача не настроена."""
    return env_value(OPERATOR_CHAT_ENV)


def enabled() -> bool:
    """Настроена ли пересылка обращений живому человеку."""
    return bool(operator_chat())


def is_operator_chat(chat_id: int | str | None,
                     username: str | None = None) -> bool:
    """Это тот самый операторский чат?

    Сообщения из него не идут обычным путём поиска: оператор пишет
    абоненту, а не боту. Сравниваем и по id, и по `@имени` — в
    переменную окружения человек может положить и то и другое.
    """
    target = operator_chat()
    if not target:
        return False
    if str(chat_id).strip() == target:
        return True
    if username and target.startswith("@"):
        return target[1:].casefold() == str(username).lstrip("@").casefold()
    return False


def chat_address(value: str) -> int | str:
    """Адрес чата в том виде, в котором его ждёт Telegram: id или @имя."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# --------------------------------------------------------------------------
# Обращение и память процесса
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Ticket:
    """Одно обращение, переданное оператору.

    `chat_id` нужен только боту — чтобы доставить ответ. В карточку он
    не печатается: оператор отвечает реплаем, и адрес ему не нужен.
    """

    number: str
    chat_id: str
    lang: str
    reason: str
    question: str
    faq_id: int | None = None
    confidence: float = 0.0
    note: str | None = None
    at: float = 0.0


@dataclass(frozen=True)
class _Question:
    """Последний вопрос чата: текст и время."""

    text: str
    at: float


class HandoffDesk:
    """Память передач: пойманные обращения и карточки в чате оператора.

    Три маленьких хранилища, все в памяти процесса — базы данных в
    задаче нет.

    * `_questions` — последний вопрос каждого чата. Нужен кнопке
      «0 Оператор»: у нажатия своего текста нет, а оператору важно
      видеть, с чем абонент пришёл.
    * `_pending` — обращение, которое чистый слой экранов уже решил
      передать, но отправка ещё не случилась: отправка асинхронная и
      живёт в `bot.py`.
    * `_cards` — связь «`message_id` карточки → обращение». По ней
      реплай оператора находит чат абонента.

    Размер ограничен :data:`MAX_TICKETS`: старые карточки вытесняются
    первыми, чтобы долгий прогон бота не съедал память.
    """

    def __init__(self, limit: int = MAX_TICKETS,
                 clock: Callable[[], float] = time.time) -> None:
        self._limit = max(1, int(limit))
        self._clock = clock
        self._counter = 0
        self._questions: dict[str, _Question] = {}
        self._pending: dict[str, Ticket] = {}
        self._cards: OrderedDict[int, Ticket] = OrderedDict()

    # --- последний вопрос чата -------------------------------------------

    def note_question(self, chat_id: int | str | None, text: str) -> None:
        """Запомнить, о чём абонент спросил последним."""
        question = (text or "").strip()
        if not question:
            return
        self._questions[str(chat_id)] = _Question(question, self._clock())

    def last_question(self, chat_id: int | str | None) -> str:
        """Свежий последний вопрос чата или пустая строка."""
        known = self._questions.get(str(chat_id))
        if known is None:
            return ""
        if self._clock() - known.at > QUESTION_TTL:
            self._questions.pop(str(chat_id), None)
            return ""
        return known.text

    # --- обращение --------------------------------------------------------

    def remember(self, chat_id: int | str | None, reason: str,
                 lang: str = config.DEFAULT_LANGUAGE,
                 question: str = "",
                 faq_id: int | None = None,
                 confidence: float = 0.0,
                 note: str | None = None) -> Ticket | None:
        """Завести обращение и отложить его до отправки.

        Возвращает `None`, когда пересылка не настроена: номера в этом
        случае не тратятся, и память не растёт. Экран абонента от этого
        не зависит — он одинаков в обоих режимах, разница только в
        строке про ответ оператора.
        """
        if not enabled():
            return None
        text = (question or "").strip() or self.last_question(chat_id)
        if len(text) > QUESTION_LENGTH:
            text = text[:QUESTION_LENGTH].rstrip() + "…"
        self._counter += 1
        ticket = Ticket(
            number=f"{self._counter:04d}",
            chat_id=str(chat_id),
            lang=lang if lang in config.LANGUAGES else config.DEFAULT_LANGUAGE,
            reason=str(reason or ""),
            question=text,
            faq_id=faq_id,
            confidence=float(confidence or 0.0),
            note=note,
            at=self._clock(),
        )
        # Одно обращение на чат: если абонент нажал оператора дважды,
        # человеку нужна последняя версия, а не обе.
        self._pending[str(chat_id)] = ticket
        return ticket

    def pending(self, chat_id: int | str | None) -> Ticket | None:
        """Обращение, которое ждёт отправки (без изъятия) — для тестов."""
        return self._pending.get(str(chat_id))

    def take(self, chat_id: int | str | None) -> Ticket | None:
        """Забрать отложенное обращение: дальше его отправляет `bot.py`."""
        return self._pending.pop(str(chat_id), None)

    # --- карточки в операторском чате -------------------------------------

    def bind(self, message_id: int | None, ticket: Ticket) -> None:
        """Запомнить карточку: по её `message_id` найдётся чат абонента."""
        if message_id is None:
            logger.warning("карточка обращения %s без message_id: реплай "
                           "оператора доставить будет некуда", ticket.number)
            return
        self._cards[int(message_id)] = ticket
        self._cards.move_to_end(int(message_id))
        while len(self._cards) > self._limit:
            self._cards.popitem(last=False)

    def find(self, message_id: int | None) -> Ticket | None:
        """Обращение по карточке, на которую ответил оператор."""
        if message_id is None:
            return None
        return self._cards.get(int(message_id))

    def clear(self) -> None:
        """Забыть всё (перезапуск, тесты)."""
        self._counter = 0
        self._questions.clear()
        self._pending.clear()
        self._cards.clear()

    def forget(self, chat_id: int | str | None) -> None:
        """Забыть один чат абонента: /reset и уборка."""
        key = str(chat_id)
        self._questions.pop(key, None)
        self._pending.pop(key, None)


#: Общая память процесса. Создаётся один раз: связь «карточка ↔ абонент»
#: обязана быть одна на весь бот.
DESK = HandoffDesk()


# --------------------------------------------------------------------------
# Карточка обращения и расписки для оператора
# --------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _t(language: str, ru: str, ky: str) -> str:
    """Подпись карточки на языке операторского чата."""
    return ky if language == "ky" else ru


def _when(moment: float, language: str) -> str:
    """Время обращения в читаемом виде, по часам сервера."""
    try:
        return time.strftime("%d.%m %H:%M", time.localtime(moment or 0.0))
    except (OSError, OverflowError, ValueError):   # pragma: no cover
        return _t(language, "время неизвестно", "убактысы белгисиз")


def _bot_result(ticket: Ticket, language: str) -> str:
    """Что успел ответить бот: тема, её формулировка и уверенность.

    Нужно, чтобы оператор не начал разговор с того же совета. Поэтому
    «ответа не нашёл» печатается тоже — это не пустая строка, а важная
    для человека улика.
    """
    if ticket.faq_id is None:
        return _t(language, "ответа не нашёл", "жооп таппады")
    from .answer import entry_by_id

    entry = entry_by_id(int(ticket.faq_id))
    title = entry.question(ticket.lang) if entry is not None else ""
    head = _t(language, f"тема {ticket.faq_id}", f"{ticket.faq_id}-тема")
    confidence = _t(language, "уверенность", "ишеним")
    shown = f"{head} · {title}" if title else head
    return f"{shown} · {confidence} {ticket.confidence:.2f}"


def card(ticket: Ticket,
         language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Карточка обращения для операторского чата.

    Персональных данных абонента в карточке нет: ни имени, ни
    `username`, ни `chat_id` — только номер обращения. Ответ доставляет
    бот по механизму реплая, поэтому адрес абоненту ничего не добавляет,
    а собирать лишнее о человеке мы не имеем права.
    """
    head = _t(language, "Обращение", "Кайрылуу")
    lines = [f"🧑‍💼 <b>{_esc(head)} {_esc(ticket.number)}</b>"]
    if ticket.reason:
        lines.append(f"<i>{_esc(reason_label(ticket.reason, language))}</i>")
    if ticket.note:
        lines.append(f"<i>{_esc(message(ticket.note, language))}</i>")

    language_name = _t(language, "язык абонента", "абонент тили")
    names = {"ru": "Русский", "ky": "Кыргызча"}
    lines.append("")
    lines.append(f"{_esc(language_name)}: <b>{_esc(names.get(ticket.lang, ticket.lang))}</b>")

    question_label = _t(language, "вопрос", "суроо")
    question = ticket.question or _t(language, "(текста нет: нажата кнопка)",
                                     "(текст жок: баскыч басылды)")
    lines.append(f"{_esc(question_label)}: «{_esc(question)}»")

    bot_label = _t(language, "бот ответил", "бот жооп берди")
    lines.append(f"{_esc(bot_label)}: {_esc(_bot_result(ticket, language))}")

    time_label = _t(language, "время", "убакыт")
    lines.append(f"{_esc(time_label)}: {_esc(_when(ticket.at, language))}")

    lines.append("")
    lines.append(f"<i>{_esc(message('operator_card_hint', language))}</i>")
    return Screen("\n".join(lines))


def notice(name: str, language: str = config.DEFAULT_LANGUAGE,
           ticket: Ticket | None = None) -> Screen:
    """Короткая расписка оператору: доставлено, не доставлено, не помню.

    Кнопок нет намеренно: операторский чат — рабочее место человека, а
    не экран бота, и инлайн-клавиатура там только мешает.
    """
    text = _esc(message(name, language))
    if ticket is not None:
        text = f"{text} <code>{_esc(ticket.number)}</code>"
    return Screen(text)
