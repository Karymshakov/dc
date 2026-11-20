"""Тесты слоя бота: флуд, склейка реплик, приветствия, выбор языка.

Четыре дефекта, найденные живым тестированием в Telegram, проверяются
здесь без самого Telegram: `bot.screen_for_text()` и
`bot.screen_for_action()` — чистые функции, а склейка сообщений написана
на обычных задачах asyncio.

Главное ограничение проекта, которое эти тесты тоже охраняют: контракт
`answer()` не меняется. Приветствия и болтовня живут в слое бота, а
`answer("салам")` по-прежнему отдаёт `faq_id = null`.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys

import pytest

from faq_bot import bot, config, presentation
from faq_bot.answer import answer, entry_by_id, stats_snapshot

OUT_OF_SCOPE = "Какая погода будет в Бишкеке в субботу?"


class _Clock:
    """Управляемые часы для окон троттлинга."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _text(screen) -> str:
    """Текст экрана без разметки."""
    assert screen is not None
    return presentation.plain_text(screen)


# --------------------------------------------------------------------------
# Дефект 1: флуд
# --------------------------------------------------------------------------

def test_throttle_warns_once_and_then_keeps_silent():
    """Лимит ответов: предупреждение одно, дальше тишина."""
    clock = _Clock()
    guard = bot.ThrottleGuard(limit=3, window=10, clock=clock)

    assert [guard.check("a") for _ in range(3)] == ["ok", "ok", "ok"]
    assert guard.check("a") == "warn"
    assert guard.check("a") == "mute"
    assert guard.check("a") == "mute"


def test_throttle_releases_after_window():
    """Это ограничитель, а не бан: окно уехало — чат снова отвечает."""
    clock = _Clock()
    guard = bot.ThrottleGuard(limit=2, window=10, clock=clock)
    for _ in range(4):
        guard.check("a")

    clock.advance(11)

    assert guard.check("a") == "ok"


def test_throttle_counts_each_chat_separately():
    """Флуд одного абонента не затыкает остальных."""
    clock = _Clock()
    guard = bot.ThrottleGuard(limit=1, window=10, clock=clock)
    guard.check("a")
    guard.check("a")

    assert guard.check("a") == "mute"
    assert guard.check("b") == "ok"


def test_flood_gets_one_warning_and_then_nothing():
    """Семь сообщений подряд не дают семи карточек."""
    bot._throttle = bot.ThrottleGuard(limit=2, window=10, clock=_Clock())

    first = bot.screen_for_text("Как проверить баланс?", 1)
    second = bot.screen_for_text("Как пополнить счёт?", 1)
    third = bot.screen_for_text("Как сменить тариф?", 1)
    rest = [bot.screen_for_text("Как узнать свой номер?", 1)
            for _ in range(4)]

    assert entry_by_id(1).answer_ru in _text(first)
    assert entry_by_id(11).answer_ru in _text(second)
    assert "Слишком много сообщений" in _text(third)
    assert third.rows == ()              # короткое предупреждение, без кнопок
    assert rest == [None, None, None, None]


def test_very_long_message_is_not_parsed_as_question():
    """Длинная простыня — не вопрос абонента: просим короче."""
    long_text = "Здравствуйте, у меня такая ситуация. " * 40
    assert len(long_text) > presentation.rules().limits.max_question_chars

    screen = bot.screen_for_text(long_text, 2)

    assert "слишком длинное" in _text(screen)
    assert entry_by_id(1).answer_ru not in _text(screen)
    assert stats_snapshot().total == 0   # разбора не было — и в статистике тоже


def test_long_message_limit_comes_from_config():
    """Порог длины берётся из bot_rules.json, а не из кода."""
    assert presentation.rules().limits.max_question_chars == 1000


# --------------------------------------------------------------------------
# Дефект 2: склейка подряд идущих сообщений
# --------------------------------------------------------------------------

def _collect(window: float, chats: list[tuple[int, str]]) -> list[tuple]:
    """Прогнать реплики через буфер и вернуть всё, что он отправил."""
    sent: list[tuple] = []

    async def scenario() -> None:
        buffer = bot.MessageBuffer(window=window)
        tasks = []
        for chat_id, text in chats:
            async def flush(joined: str, chat_id: int = chat_id) -> None:
                sent.append((chat_id, joined))

            tasks.append(buffer.push(chat_id, text, flush))
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    return sent


def test_three_messages_in_a_row_get_one_answer():
    """Одна проблема тремя репликами — один ответ на склеенный текст."""
    sent = _collect(0.05, [
        (7, "привет"),
        (7, "у меня там не работает интернет"),
        (7, "можете сказать что случилось"),
    ])

    assert len(sent) == 1
    assert sent[0][1] == ("привет у меня там не работает интернет "
                          "можете сказать что случилось")


def test_single_message_behaves_as_before():
    """Одна реплика за окно — поведение не меняется."""
    sent = _collect(0.05, [(7, "Как проверить баланс?")])

    assert sent == [(7, "Как проверить баланс?")]


def test_buffer_does_not_mix_chats():
    """Реплики разных чатов не склеиваются между собой."""
    sent = _collect(0.05, [(1, "не работает интернет"), (2, "как сменить тариф")])

    assert sorted(sent) == [(1, "не работает интернет"), (2, "как сменить тариф")]


def test_merged_question_finds_the_topic():
    """Склеенный текст разбирается как один вопрос и находит тему."""
    joined = bot.MessageBuffer.join(["привет", "у меня там не работает интернет",
                                     "можете сказать что случилось"])
    screen = bot.screen_for_text(joined, 3)

    assert entry_by_id(3).answer_ru in _text(screen)


def test_merge_window_is_configured_in_rules():
    """Окно склейки задано конфигом и лежит в разрешённых пределах."""
    window = presentation.rules().limits.merge_window_seconds

    assert 1.5 <= window <= 2.5
    assert bot.MessageBuffer().window == window


def test_buffer_ignores_empty_replies():
    """Пустые реплики не рождают пустого ответа."""
    sent = _collect(0.05, [(5, "   "), (5, "")])

    assert sent == []


class _FakeMessage:
    """Сообщение Telegram в объёме, который нужен обработчику."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)


class _FakeUpdate:
    """Обновление Telegram: чат и сообщение, больше обработчику не нужно."""

    def __init__(self, chat_id: int, text: str) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = _FakeMessage(text)


class _FakeQuery:
    """`callback_query` в объёме, который нужен обработчику кнопок.

    Считает вызовы `answer()` (это `answerCallbackQuery`, без него у
    кнопки крутятся часики) и запоминает правки сообщения. Отдельно
    хранит `message.replies`: если там что-то появилось, значит нажатие
    кнопки прислало новое сообщение, а это и есть главный дефект.
    """

    def __init__(self, data: str, error: BaseException | None = None) -> None:
        self.data = data
        self.answered = 0
        self.edits: list[tuple[str, object]] = []
        self.message = _FakeMessage("прежнее сообщение")
        self._error = error

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answered += 1

    async def edit_message_text(self, text: str, **kwargs: object) -> None:
        if self._error is not None:
            raise self._error
        self.edits.append((text, kwargs.get("reply_markup")))


class _FakeButtonUpdate:
    """Обновление с нажатой кнопкой."""

    def __init__(self, chat_id: int, query: _FakeQuery) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = query.message
        self.callback_query = query


def _press(chat_id: int, action: str,
           error: BaseException | None = None) -> _FakeQuery:
    """Нажать кнопку через настоящий обработчик бота."""
    query = _FakeQuery(action, error)
    asyncio.run(bot._on_button(_FakeButtonUpdate(chat_id, query), None))
    return query


def _run_handler(chat_id: int, texts: list[str],
                 window: float = 0.05) -> list[str]:
    """Прогнать реплики через настоящий обработчик бота."""
    bot._buffer = bot.MessageBuffer(window=window)
    updates = [_FakeUpdate(chat_id, text) for text in texts]

    async def scenario() -> None:
        for update in updates:
            await bot._on_text(update, None)
        await bot.buffer().drain()

    asyncio.run(scenario())
    return [reply for update in updates
            for reply in update.effective_message.replies]


def test_handler_answers_once_for_three_messages():
    """Сквозная проверка: три реплики в окне — одно сообщение в чат."""
    pytest.importorskip("telegram")

    replies = _run_handler(51, ["привет",
                                "у меня там не работает интернет",
                                "можете сказать что случилось"])

    assert len(replies) == 1
    assert entry_by_id(3).answer_ru in replies[0]


def test_handler_answers_once_for_a_single_message():
    """Одна реплика — один ответ, как и раньше."""
    pytest.importorskip("telegram")

    replies = _run_handler(52, ["Как проверить баланс?"])

    assert len(replies) == 1
    assert entry_by_id(1).answer_ru in replies[0]


def test_pasted_text_split_by_telegram_gets_one_polite_reply():
    """Тот самый дефект: Telegram разрезал вставленный текст на семь частей.

    Части склеиваются в один текст, он оказывается длиннее порога — и
    абонент получает одну вежливую просьбу написать короче вместо семи
    красных карточек.
    """
    pytest.importorskip("telegram")
    part = "Здравствуйте, расскажу свою ситуацию по порядку. " * 4

    replies = _run_handler(54, [part] * 7)

    assert len(replies) == 1
    assert "слишком длинное" in replies[0]


def test_handler_keeps_silence_when_throttled():
    """Заглушённый троттлингом чат не получает ничего."""
    pytest.importorskip("telegram")
    bot._throttle = bot.ThrottleGuard(limit=1, window=10)

    first = _run_handler(53, ["Как проверить баланс?"])
    second = _run_handler(53, ["Как пополнить счёт?"])
    third = _run_handler(53, ["Как сменить тариф?"])

    assert len(first) == 1
    assert "Слишком много сообщений" in second[0]
    assert third == []


def test_sloppy_three_part_question_gets_exactly_one_answer():
    """Живой тест: «крч проблема такая» / «типа сигнал есть» / «но инета нет».

    Три реплики в окне склейки — ровно один ответ, а не три карточки.
    """
    pytest.importorskip("telegram")

    replies = _run_handler(55, ["крч проблема такая",
                                "типа сигнал есть",
                                "но инета нет"])

    assert len(replies) == 1
    assert entry_by_id(3).answer_ru in replies[0]


def test_merging_does_not_depend_on_job_queue():
    """Склейка живёт на asyncio, а не на `job_queue`.

    В установленной версии python-telegram-bot `application.job_queue`
    равен None: планировщик живёт в отдельном дополнении
    (`pip install "python-telegram-bot[job-queue]"`). На `job_queue`
    склейка молча не работала бы именно там, где её проверяют.
    """
    pytest.importorskip("telegram")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        application = bot.build_application(
            "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert application.job_queue is None
    assert "job_queue" not in bot.MessageBuffer.push.__doc__.lower()
    assert len(_run_handler(56, ["не работает", "интернет"])) == 1


# --------------------------------------------------------------------------
# Дефект 5: нажатие кнопки переписывает сообщение, а не плодит новые
# --------------------------------------------------------------------------

def test_button_press_edits_the_message_and_sends_nothing():
    """Нажатие раздела меню превращает то же сообщение в список тем."""
    pytest.importorskip("telegram")

    query = _press(61, "section:2")

    assert query.answered == 1               # без этого крутятся часики
    assert len(query.edits) == 1
    assert query.message.replies == []       # новых сообщений нет
    assert entry_by_id(3).question_ru in query.edits[0][0]


def test_topic_button_turns_the_message_into_a_card():
    """Нажатие темы превращает сообщение в карточку ответа."""
    pytest.importorskip("telegram")

    query = _press(62, "faq:4")

    assert query.message.replies == []
    assert entry_by_id(4).answer_ru in query.edits[0][0]


def test_menu_button_returns_the_message_to_the_menu():
    """«Меню» возвращает то же сообщение в меню."""
    pytest.importorskip("telegram")

    _press(63, "faq:4")
    query = _press(63, "menu")

    assert query.message.replies == []
    for section in presentation.MENU_SECTIONS:
        assert section.name("ru") in query.edits[0][0]


def test_ack_collapses_the_card_into_one_line():
    """«Понятно» сворачивает карточку в одну строку: без меню и без кнопок.

    Раньше одно касание давало три сообщения: «Жакшы! Отлично!», а
    следом полное меню.
    """
    pytest.importorskip("telegram")

    query = _press(64, "ack")
    text, markup = query.edits[0]

    assert query.message.replies == []
    assert "\n" not in text
    assert markup is None                    # клавиатура тоже убрана
    assert "Баланс и оплата" not in text


def test_language_button_redraws_the_same_card():
    """🇰🇬 на карточке даёт тот же ответ по-кыргызски, а не меню."""
    pytest.importorskip("telegram")

    card = bot.screen_for_action("faq:4", 65)
    language_actions = [button.action for button in card.buttons
                        if button.action.startswith("lang:")]
    query = _press(65, language_actions[0])
    text = query.edits[0][0]

    assert language_actions == ["lang:ky:faq:4"]
    assert query.message.replies == []
    assert entry_by_id(4).answer_ky in text
    assert entry_by_id(4).answer_ru not in text
    assert bot.chat_language(65) == "ky"


def test_unmodified_screen_is_not_an_error():
    """Telegram отклоняет правку тем же текстом — это не ошибка.

    Абонент нажал кнопку экрана, на котором уже стоит. Бот обязан
    промолчать, а не падать и не присылать новое сообщение.
    """
    telegram = pytest.importorskip("telegram")

    error = telegram.error.BadRequest(
        "Message is not modified: specified new message content and reply "
        "markup are exactly the same")
    query = _press(66, "menu", error)

    assert bot._is_not_modified(error)
    assert query.answered == 1
    assert query.edits == []
    assert query.message.replies == []


def test_failed_edit_does_not_fall_back_to_a_new_message():
    """Правка не удалась — в лог, но не новым сообщением в чат."""
    telegram = pytest.importorskip("telegram")

    query = _press(67, "menu",
                   telegram.error.BadRequest("Message to edit not found"))

    assert query.message.replies == []


def test_edit_reports_what_happened():
    """`_edit()` возвращает состояние: оно нужно логу и тестам."""
    telegram = pytest.importorskip("telegram")
    screen = presentation.menu_screen("ru")

    def status(error: BaseException | None) -> str:
        query = _FakeQuery("menu", error)
        return asyncio.run(bot._edit(query, screen))

    assert status(None) == "edited"
    assert status(telegram.error.BadRequest("Message is not modified")) \
        == "unchanged"
    assert status(telegram.error.BadRequest("Message can't be edited")) \
        == "failed"


def test_commands_still_send_a_new_message():
    """Команда — это новая реплика абонента, на неё отвечаем сообщением."""
    pytest.importorskip("telegram")

    for handler in (bot._on_start, bot._on_help, bot._on_stats, bot._on_reset):
        update = _FakeUpdate(69, "/команда")
        asyncio.run(handler(update, None))

        assert len(update.effective_message.replies) == 1, handler.__name__


def test_a_minute_of_pressing_buttons_adds_no_messages():
    """Двадцать касаний — ни одного нового сообщения в чате.

    Это и есть проверка главного дефекта: за минуту живого теста чат
    превращался в простыню, в которой не найти ответ.
    """
    pytest.importorskip("telegram")
    actions = ["section:1", "faq:1", "ack", "menu", "section:5", "faq:4",
               "lang:ky:faq:4", "another", "menu", "faq:11"]

    pressed = [_press(68, action) for action in actions * 2]

    assert sum(len(query.message.replies) for query in pressed) == 0
    assert sum(len(query.edits) for query in pressed) == len(pressed)
    assert all(query.answered == 1 for query in pressed)


# --------------------------------------------------------------------------
# Дефект 3: приветствия и болтовня
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "салам", "саламатсызбы", "Привет", "здравствуйте", "assalam", "salam",
    "Добрый день",
])
def test_greeting_gets_a_greeting(text):
    """Приветствие получает приветствие, а не 🔴 «ответа не нашёл»."""
    rendered = _text(bot.screen_for_text(text, 11))

    assert "не нашёл" not in rendered
    assert "таппадым" not in rendered
    assert ("Здравствуйте" in rendered or "Саламатсызбы" in rendered)


@pytest.mark.parametrize("text", [
    "кто ты", "как дела? кто ты", "а как ты работаешь", "что умеешь",
    "сен кимсиң", "эмне кыла аласың",
])
def test_question_about_bot_gets_an_honest_answer(text):
    """Вопрос о боте: справочная INAI Mobile по 15 темам, без выдумок."""
    rendered = _text(bot.screen_for_text(text, 12))

    assert "не нашёл" not in rendered
    assert "15" in rendered
    assert "INAI Mobile" in rendered


@pytest.mark.parametrize("text, marker", [
    ("спасибо", "Пожалуйста"),
    ("рахмат", "Арзыбайт"),
    ("чоң рахмат", "Арзыбайт"),
])
def test_thanks_gets_a_short_answer(text, marker):
    """Благодарность — короткий ответ."""
    rendered = _text(bot.screen_for_text(text, 13))

    assert marker in rendered
    assert "не нашёл" not in rendered


@pytest.mark.parametrize("text", ["пока", "до свидания", "кош болуңуз"])
def test_farewell_gets_a_short_answer(text):
    """Прощание — короткий ответ."""
    rendered = _text(bot.screen_for_text(text, 14))

    assert "не нашёл" not in rendered
    assert ("Всего доброго" in rendered or "Жакшы калыңыз" in rendered)


def test_smalltalk_answers_in_language_of_the_message():
    """Отвечаем на языке реплики: «салам» — по-кыргызски, «привет» — по-русски."""
    kyrgyz = _text(bot.screen_for_text("саламатсызбы", 15))
    russian = _text(bot.screen_for_text("здравствуйте", 16))

    assert kyrgyz.startswith("👋 Саламатсызбы")
    assert russian.startswith("👋 Здравствуйте")


def test_greeting_with_a_real_question_is_still_a_question():
    """«привет, не работает интернет» — это вопрос, а не болтовня."""
    screen = bot.screen_for_text("привет, у меня не работает интернет", 17)

    assert entry_by_id(3).answer_ru in _text(screen)


@pytest.mark.parametrize("text", [
    "салам", "привет", "как дела? кто ты", "а как ты работаешь",
    "спасибо", "рахмат", "пока",
])
def test_smalltalk_does_not_change_the_answer_contract(text):
    """Приветствия и болтовня вне базы FAQ: `answer()` отдаёт faq_id = null.

    Это железное ограничение проекта: по `answer()` считается
    автопроверка жюри, и человеческие ответы на болтовню живут только
    в слое бота.
    """
    result = answer(text)

    assert result["faq_id"] is None
    assert result["lang"] in config.LANGUAGES


def test_detect_intent_ignores_topic_words():
    """Короткая реплика со своим ключевым словом темы — не болтовня."""
    assert presentation.detect_intent("салам") == "greeting"
    assert presentation.detect_intent("роумингди кантип кошом") is None
    assert presentation.detect_intent("салам, интернет иштебейт") is None


# --------------------------------------------------------------------------
# Дефект 4: переключатель языка
# --------------------------------------------------------------------------

def test_language_button_pins_the_choice_and_shows_it():
    """Кнопка сохраняет выбор и показывает его самим текстом экрана.

    Отдельной строки состояния больше нет: меню написано на выбранном
    языке, и этого достаточно. Подпись кнопки называет язык, на который
    переключит нажатие, а не текущий.
    """
    screen = bot.screen_for_action("lang:ky", 21)
    rendered = _text(screen)

    assert bot.chat_language(21) == "ky"
    assert bot.is_language_pinned(21)
    assert "кыргызча жооп берем" in rendered
    assert "Тирүү оператор" in rendered        # меню стало кыргызским
    assert "Живой оператор" not in rendered
    labels = [button.label for button in screen.buttons
              if button.action.startswith("lang:")]
    assert labels == ["🇷🇺 Русский"]


def test_pinned_language_survives_several_messages():
    """Выбор языка держится, а не слетает на следующем сообщении."""
    bot.screen_for_action("lang:ky", 22)

    for text in ("баланс", "*100#", "пакет"):
        screen = bot.screen_for_text(text, 22)
        labels = [button.label for button in screen.buttons]

        assert bot.chat_language(22) == "ky"
        assert "✅ Понятно" not in labels   # подписи кнопок тоже кыргызские

    rendered = _text(bot.screen_for_text("баланс", 22))
    assert entry_by_id(1).answer_ky in rendered
    assert entry_by_id(1).answer_ru not in rendered


def test_answer_is_written_only_in_the_language_of_the_question():
    """ТЗ: «отвечает на том же языке». Один язык, без второго перевода."""
    kyrgyz = _text(bot.screen_for_text("Балансты кантип текшерем?", 31))
    russian = _text(bot.screen_for_text("Как проверить баланс?", 32))

    assert entry_by_id(1).answer_ky in kyrgyz
    assert entry_by_id(1).answer_ru not in kyrgyz
    assert entry_by_id(1).answer_ru in russian
    assert entry_by_id(1).answer_ky not in russian


def test_language_button_redraws_help_and_not_the_menu():
    """Кнопка языка на справке перерисовывает справку, а не шлёт меню."""
    screen = bot.screen_for_action("help", 70)
    actions = [button.action for button in screen.buttons
               if button.action.startswith("lang:")]
    redrawn = _text(bot.screen_for_action("lang:ky:help", 70))

    assert actions == ["lang:ky:help"]
    assert "Жардам" in redrawn
    assert "Помощь" not in redrawn


@pytest.mark.parametrize("action", [
    "lang:", "lang:xx", "lang:ky:lang:ru", "lang:ru:какая-то чепуха",
    "lang", "faq:999", "section:9",
])
def test_broken_button_falls_back_instead_of_crashing(action):
    """Битая или устаревшая кнопка не роняет бота и не зацикливает его.

    Повторный `lang:` внутри адреса игнорируется: он означал бы
    бесконечную перерисовку.
    """
    screen = bot.screen_for_action(action, 71)

    assert screen.text
    assert screen.rows


def test_pinned_language_does_not_leak_between_chats():
    """Выбор языка одного чата не виден другому."""
    bot.screen_for_action("lang:ky", 23)

    assert bot.chat_language(23) == "ky"
    assert bot.chat_language(24) == config.DEFAULT_LANGUAGE
    assert entry_by_id(1).answer_ru in _text(
        bot.screen_for_text("Как проверить баланс?", 24))


def test_button_answer_uses_pinned_language():
    """Нажатие похожего вопроса тоже отвечает на выбранном языке."""
    bot.screen_for_action("lang:ky", 25)

    assert entry_by_id(1).answer_ky in _text(bot.screen_for_action("faq:1", 25))


def test_clear_other_language_is_answered_in_it_but_keeps_the_choice():
    """Явно русский вопрос получает русский ответ, выбор кнопкой остаётся.

    Отвечать по-кыргызски человеку, который по-русски спросил «как
    проверить баланс», хуже, чем не заметить кнопку: одна реплика может
    быть цитатой или копипастой.
    """
    bot.screen_for_action("lang:ky", 26)

    assert bot.reply_language(26, "Как проверить баланс?") == "ru"
    assert bot.is_language_pinned(26)
    assert bot.chat_language(26) == "ky"


def test_two_messages_in_another_language_release_the_choice():
    """Две явные реплики подряд на другом языке снимают выбор кнопкой."""
    bot.screen_for_action("lang:ky", 27)

    bot.reply_language(27, "Как проверить баланс?")
    bot.reply_language(27, "Как пополнить счёт?")

    assert not bot.is_language_pinned(27)
    assert bot.chat_language(27) == "ru"


def test_weak_signal_does_not_release_the_choice():
    """Нейтральная реплика («баланс», «2») выбор языка не отменяет."""
    bot.screen_for_action("lang:ky", 28)

    for text in ("баланс", "2", "ооба", "роуминг"):
        assert bot.reply_language(28, text) == "ky"

    assert bot.is_language_pinned(28)


def test_reset_forgets_the_language_choice():
    """/reset возвращает автоопределение."""
    bot.screen_for_action("lang:ky", 29)
    bot.forget_chat(29)

    assert not bot.is_language_pinned(29)
    assert bot.chat_language(29) == config.DEFAULT_LANGUAGE


def test_language_detection_still_works_without_the_button():
    """Без кнопки всё как раньше: язык определяется по тексту."""
    assert "Балансты" in _text(
        bot.screen_for_text("Балансты кантип текшерем?", 30))
    assert bot.chat_language(30) == "ky"


# --------------------------------------------------------------------------
# Конфигурация поведения: bot_rules.json
# --------------------------------------------------------------------------

def test_rules_are_loaded_from_json():
    """Правила читаются из файла, а не из кода."""
    rules = presentation.rules()

    assert rules.source == "bot_rules.json"
    assert rules.operator.faq_id == 14
    assert rules.operator.repeated_miss_threshold == 3
    assert rules.limits.replies_per_window >= 1


def test_every_message_has_both_languages():
    """Один забытый перевод оставил бы без ответа половину аудитории."""
    for name, texts in presentation.rules().messages.items():
        for language in config.LANGUAGES:
            assert texts.get(language), (name, language)


def test_doc_keys_are_ignored_but_stay_in_the_file():
    """Ключи `_doc` — документация для человека: код их не читает и не трёт."""
    raw = json.loads(presentation.RULES_PATH.read_text(encoding="utf-8"))

    assert "_doc" in raw
    for section in presentation.rules().detection.values():
        assert all(not word.startswith("_doc") for word in section)
    assert "_doc" not in presentation.rules().messages
    assert "_doc" not in presentation.rules().operator.reason_labels


def test_missing_rules_file_does_not_break_the_bot(tmp_path):
    """Нет конфига — работают встроенные значения, а не трассировка."""
    rules = presentation.rules(tmp_path / "нет-такого-файла.json")

    assert rules.source == "встроенные значения"
    assert rules.limits.max_question_chars > 0
    assert rules.messages["greeting"]["ky"]


def test_broken_rules_file_does_not_break_the_bot(tmp_path):
    """Битый JSON — предупреждение в лог и встроенные значения."""
    broken = tmp_path / "bot_rules.json"
    broken.write_text("{это не json", encoding="utf-8")

    rules = presentation.rules(broken)

    assert rules.source == "встроенные значения"
    assert rules.operator.faq_id == 14


def test_rules_are_cached():
    """Файл читается один раз: конфиг не дёргаем на каждое сообщение."""
    assert presentation.rules() is presentation.rules()
    presentation.reload_rules()
    assert presentation.rules().source == "bot_rules.json"


def test_word_matching_uses_normalized_prefix():
    """Сравнение по нормализованной форме и по началу слова."""
    assert presentation.matches_pattern("саламатсызбы", "саламатсыз")
    assert presentation.matches_pattern("тируу оператор керек",
                                        "тирүү оператор")
    assert presentation.matches_pattern("хочу написать жалобу", "жалоб")
    # Короткое слово сравнивается целиком, иначе «кош» поймало бы «кошом».
    assert not presentation.matches_pattern("кантип кошом", "кош")


# --------------------------------------------------------------------------
# Оператор и повторные отказы
# --------------------------------------------------------------------------

def test_operator_word_still_reaches_operator():
    """Бонус ТЗ на месте: слово «оператор» ведёт к живому человеку."""
    rendered = _text(bot.screen_for_text("оператор", 41))

    assert entry_by_id(14).answer_ru in rendered
    assert "Попросили оператора" in rendered


@pytest.mark.parametrize("text, label", [
    ("хочу написать жалобу", "Жалоба или претензия"),
    ("верните деньги немедленно", "Возврат средств"),
])
def test_complaints_go_to_operator_with_reason(text, label):
    """Жалобы и возвраты бот сам не разбирает — передаёт человеку."""
    rendered = _text(bot.screen_for_text(text, 42))

    assert label in rendered


def test_escalation_keeps_the_answer_from_the_base():
    """«Украли телефон, блокируйте сим» — и инструкция темы 13, и оператор.

    Инструкция вместо человека была бы отпиской, человек вместо
    инструкции — потерей готового ответа из faq.json.
    """
    screen = bot.screen_for_text("Украли телефон, блокируйте сим", 46)
    rendered = _text(screen)

    assert entry_by_id(13).answer_ru in rendered
    assert "решает оператор" in rendered
    assert "operator" in screen.actions()


def test_repeated_misses_offer_an_operator():
    """После трёх отказов подряд бот сам предлагает оператора."""
    screens = [bot.screen_for_text(OUT_OF_SCOPE, 43) for _ in range(3)]
    rendered = _text(screens[-1])

    assert "не понимаю вопрос" in rendered
    assert "operator" in screens[-1].actions()
    assert "не понимаю вопрос" not in _text(screens[0])


def test_found_answer_resets_the_miss_streak():
    """Успешный ответ обнуляет цепочку отказов."""
    bot.screen_for_text(OUT_OF_SCOPE, 44)
    bot.screen_for_text(OUT_OF_SCOPE, 44)
    bot.screen_for_text("Как проверить баланс?", 44)
    rendered = _text(bot.screen_for_text(OUT_OF_SCOPE, 44))

    assert "не понимаю вопрос" not in rendered


def test_stats_shows_operator_reasons_without_personal_data():
    """Причины передачи видны в /stats, персональных данных там нет."""
    bot.screen_for_text("оператор", 45)
    rendered = _text(bot.stats_screen(45))

    assert "передано оператору" in rendered
    assert "45" not in rendered.replace("*145*", "")
    assert "без персональных данных" in rendered


def test_stats_screen_survives_empty_history():
    """/stats на пустой истории не падает."""
    assert "пока вопросов не было" in _text(bot.stats_screen())


# --------------------------------------------------------------------------
# Границы слоя
# --------------------------------------------------------------------------

def test_run_without_a_token_explains_instead_of_crashing(capsys, monkeypatch):
    """Без токена — понятное сообщение и код 2, а не трассировка стека.

    Приложение Telegram при этом не собирается и в сеть никто не идёт:
    проверка заканчивается до `build_application()`.
    """
    monkeypatch.delenv(bot.TOKEN_ENV, raising=False)

    code = bot.run("")
    printed = capsys.readouterr().out

    assert code == 2
    assert "Не задан токен" in printed
    assert bot.TOKEN_ENV in printed
    assert "Traceback" not in printed


def test_importing_bot_does_not_pull_telegram(monkeypatch):
    """Telegram импортируется лениво: модуль бота тестируется без него."""
    for name in [item for item in sys.modules
                 if item.startswith(("faq_bot", "telegram"))]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    importlib.import_module("faq_bot.bot")

    assert not [name for name in sys.modules if name.startswith("telegram")]
