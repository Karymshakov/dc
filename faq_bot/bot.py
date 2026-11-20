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

Чем занимается именно этот слой (и не занимается `answer()`).

* **Защита от флуда.** `ThrottleGuard` ограничивает число ответов на чат
  в окно времени, а слишком длинное сообщение не разбирается как вопрос.
* **Склейка реплик.** `MessageBuffer` ждёт короткое окно тишины и
  отвечает один раз на склеенный текст: абонент часто пишет одну мысль
  тремя сообщениями.
* **Приветствия и болтовня.** «салам», «кто ты», «рахмат» получают
  человеческий ответ здесь, до вызова `answer()`. Сама функция
  `answer()` по-прежнему отдаёт на такие реплики `faq_id = null` —
  так требует спецификация, и по ней считается автопроверка.
* **Язык.** Выбор языка кнопкой сохраняется для чата и перебивает
  автоопределение; правило спора описано у `reply_language()`.

Тексты ответов, списки слов и пороги лежат в `faq_bot/bot_rules.json`
и читаются через `presentation.rules()`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import config, presentation
from .answer import (
    MEMORY,
    STATS,
    all_entries,
    detect_language,
    entry_by_id,
    reply_for_faq_id,
    reset_context,
    respond,
    stats_snapshot,
)
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
# Склейка подряд идущих сообщений
# --------------------------------------------------------------------------

class MessageBuffer:
    """Склейка подряд идущих реплик одного чата в один вопрос.

    Абонент пишет одну мысль тремя сообщениями: «привет» / «у меня там
    не работает интернет» / «можете сказать что случилось». Буфер ждёт
    окно тишины и отвечает один раз на склеенный текст. Если за окно
    пришла одна реплика, поведение не меняется — только ответ приходит
    через `window` секунд.

    Почему отложенная задача asyncio, а не `job_queue` из
    python-telegram-bot: `job_queue` живёт в отдельном дополнении
    (`pip install "python-telegram-bot[job-queue]"`, APScheduler), и в
    окружении проекта `application.job_queue` равен None — склейка молча
    не работала бы именно там, где её проверяют. Задача asyncio работает
    на голой библиотеке, а сам буфер тестируется вообще без Telegram.

    Планирование, а не ожидание: `push()` не блокирует обработчик.
    PTB по умолчанию разбирает обновления по одному, и `await sleep()`
    внутри обработчика задержал бы следующие сообщения — склеивать было
    бы нечего.
    """

    def __init__(self, window: float | None = None) -> None:
        self._window = window
        self._parts: dict[str, list[str]] = {}
        self._tokens: dict[str, int] = {}
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def window(self) -> float:
        """Окно тишины в секундах."""
        if self._window is not None:
            return float(self._window)
        return float(presentation.rules().limits.merge_window_seconds)

    @staticmethod
    def join(parts: list[str]) -> str:
        """Склеить реплики в один вопрос."""
        return " ".join(part.strip() for part in parts if part and part.strip())

    def push(self, chat_id: int | str | None, text: str,
             flush: Callable[[str], Awaitable[None]]) -> asyncio.Task[None]:
        """Положить реплику в буфер и перезапустить окно ожидания.

        Возвращает задачу: она нужна тестам, чтобы дождаться ответа.
        """
        key = _key(chat_id)
        self._parts.setdefault(key, []).append(text or "")
        token = self._tokens.get(key, 0) + 1
        self._tokens[key] = token
        task = asyncio.ensure_future(self._flush_later(key, token, flush))
        # Ссылку держим сами: иначе задачу может собрать сборщик мусора.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def _flush_later(self, key: str, token: int,
                           flush: Callable[[str], Awaitable[None]]) -> None:
        """Дождаться окна тишины и ответить один раз."""
        try:
            await asyncio.sleep(self.window)
        except asyncio.CancelledError:
            raise
        if self._tokens.get(key) != token:
            return                 # пришла новая реплика — ответит её таймер
        parts = self._parts.pop(key, [])
        self._tokens.pop(key, None)
        joined = self.join(parts)
        if not joined:
            return
        try:
            await flush(joined)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("не удалось ответить на склеенную реплику")

    def pending(self, chat_id: int | str | None) -> int:
        """Сколько реплик чата ждут отправки (для тестов и отладки)."""
        return len(self._parts.get(_key(chat_id), ()))

    def clear(self) -> None:
        """Забыть буферы всех чатов и снять ожидающие задачи."""
        self._parts.clear()
        self._tokens.clear()
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()


_buffer: MessageBuffer | None = None


def buffer() -> MessageBuffer:
    """Буфер склейки, общий для процесса."""
    global _buffer
    if _buffer is None:
        _buffer = MessageBuffer()
    return _buffer


def reset_runtime_state() -> None:
    """Забыть всё состояние слоя бота: язык, троттлинг, буферы, тему."""
    _chat_language.clear()
    _chat_language_pinned.clear()
    _chat_language_streak.clear()
    throttle().clear()
    buffer().clear()
    reset_context()


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------

def _operator_screen(chat_id: int | str | None,
                     reason: str | None = None,
                     language: str | None = None) -> Screen:
    """Экран передачи оператору; причина уходит в агрегат /stats."""
    operator = presentation.rules().operator
    if language not in config.LANGUAGES:
        language = chat_language(chat_id)
    if reason:
        STATS.record_operator(reason, chat_id)
        MEMORY.forget_misses(chat_id)
    return presentation.operator_screen(entry_by_id(operator.faq_id),
                                        language, reason)


def screen_for_text(text: str,
                    chat_id: int | str | None = None) -> Screen | None:
    """Экран в ответ на свободный ввод или цифру меню.

    Порядок веток осознанный: сначала защита от флуда, потом цифра меню
    и просьба об операторе, потом приветствия и болтовня, и только затем
    `answer()`. Так «салам» получает приветствие, а «салам, интернет
    иштебейт» — настоящий поиск по базе.

    `None` означает намеренную тишину: чат превысил лимит ответов и уже
    получил предупреждение.
    """
    stripped = (text or "").strip()
    language = reply_language(chat_id, stripped)
    pinned = is_language_pinned(chat_id)
    if not stripped:
        return presentation.menu_screen(language, pinned=pinned)

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
    if reason is not None:
        return _operator_screen(chat_id, reason, language)

    intent = presentation.detect_intent(stripped)
    if intent is not None:
        return presentation.smalltalk_screen(intent, language, pinned=pinned)

    reply = respond(stripped, chat_id=chat_id, language=language)
    set_chat_language(chat_id, reply.lang)
    screen = presentation.answer_card(reply)
    if reply.faq_id is None:
        screen = _maybe_offer_operator(screen, chat_id, reply.lang)
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


def screen_for_action(action: str, chat_id: int | str | None = None) -> Screen:
    """Экран в ответ на нажатие кнопки.

    Нажатие `faq:<id>` обязано выдать ответ именно по этой теме — это
    пункт 3 обязательной части.
    """
    command, _, argument = (action or "").partition(":")
    language = chat_language(chat_id)
    pinned = is_language_pinned(chat_id)

    if command == "faq" and argument.isdigit():
        reply = reply_for_faq_id(int(argument), language, chat_id=chat_id)
        if reply is not None:
            return presentation.answer_card(reply)
        return presentation.menu_screen(language, pinned=pinned)

    if command == "section" and argument.isdigit():
        section = presentation.section_by_number(int(argument))
        if section is None:
            return presentation.menu_screen(language, pinned=pinned)
        if section.number == 0:
            return _operator_screen(chat_id, "direct_request", language)
        return presentation.section_screen(section, all_entries(), language)

    if command == "operator":
        return _operator_screen(chat_id, "direct_request", language)
    if command == "ack":
        return presentation.acknowledged_screen(language)
    if command == "another":
        reset_context(chat_id)
        return presentation.another_question_screen(language, pinned=pinned)
    if command == "lang" and argument in config.LANGUAGES:
        set_chat_language(chat_id, argument, pinned=True)
        return presentation.language_switched_screen(argument)
    if command == "stats":
        return stats_screen(chat_id)
    return presentation.menu_screen(language, pinned=pinned)


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


async def _send(message: Any, screen: Screen) -> None:
    """Отправить экран в чат."""
    await message.reply_text(
        screen.text,
        parse_mode=screen.parse_mode,
        reply_markup=_markup(screen),
        disable_web_page_preview=True,
    )


async def _on_start(update: Any, context: Any) -> None:
    """/start — USSD-меню. Экрана выбора языка нет намеренно."""
    chat_id = update.effective_chat.id
    await _send(update.effective_message,
                presentation.start_screen(chat_language(chat_id),
                                          pinned=is_language_pinned(chat_id)))


async def _on_help(update: Any, context: Any) -> None:
    """/help — короткая справка."""
    chat_id = update.effective_chat.id
    await _send(update.effective_message,
                presentation.help_screen(chat_language(chat_id),
                                         pinned=is_language_pinned(chat_id)))


async def _on_stats(update: Any, context: Any) -> None:
    """/stats — агрегат неотвеченных вопросов."""
    chat_id = update.effective_chat.id
    await _send(update.effective_message, stats_screen(chat_id))


async def _on_reset(update: Any, context: Any) -> None:
    """/reset — забыть тему диалога и выбранный язык."""
    chat_id = update.effective_chat.id
    forget_chat(chat_id)
    await _send(update.effective_message,
                presentation.another_question_screen(chat_language(chat_id)))


async def _on_text(update: Any, context: Any) -> None:
    """Свободный вопрос абонента: один ответ на серию подряд идущих реплик."""
    message = update.effective_message
    chat_id = update.effective_chat.id

    async def flush(joined: str) -> None:
        screen = screen_for_text(joined, chat_id)
        if screen is None:
            return                      # троттлинг: намеренная тишина
        await _send(message, screen)

    buffer().push(chat_id, message.text or "", flush)


async def _on_button(update: Any, context: Any) -> None:
    """Нажатие кнопки: похожий вопрос, раздел меню, язык, оператор."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    screen = screen_for_action(query.data or "", chat_id)
    await _send(query.message, screen)


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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                           _on_text))
    return application


def run(token: str | None = None) -> int:
    """Запустить бота. Возвращает код выхода, исключений наружу не бросает."""
    token = (token or os.environ.get(TOKEN_ENV) or "").strip()
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
    logger.info("склейка реплик %.1f с, не больше %d ответов в %.0f с, "
                "вопрос до %d знаков",
                rules.limits.merge_window_seconds,
                rules.limits.replies_per_window,
                rules.limits.window_seconds,
                rules.limits.max_question_chars)
    print("Бот запущен. Остановить — Ctrl+C.")
    try:
        application.run_polling()
    except KeyboardInterrupt:              # pragma: no cover
        print("\nОстановлен.")
    return 0
