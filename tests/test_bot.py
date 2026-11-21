"""Тесты слоя бота: флуд, ход разговора, голос, приветствия, язык.

Дефекты, найденные живым тестированием в Telegram, проверяются здесь без
самого Telegram: `bot.screen_for_text()` и `bot.screen_for_action()` —
чистые функции, а обработчики вызываются с поддельным обновлением.

Два дефекта разобраны дословно по переписке. «Бот повторил свой же
ответ, когда ему сказали, что совет не помог» — группа про отказ.
«Бот вклинился на середине мысли, набранной по-кыргызски с телефона» —
группа про дозревание ответа. Голосовые проверяются с подменённым
распознаванием: настоящий ключ оплачивает человек.

Главное ограничение проекта, которое эти тесты тоже охраняют: контракт
`answer()` не меняется. Приветствия и болтовня живут в слое бота, а
`answer("салам")` по-прежнему отдаёт `faq_id = null`.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

from faq_bot import bot, config, handoff, presentation
from faq_bot.answer import answer, entry_by_id, stats_snapshot

OUT_OF_SCOPE = "Какая погода будет в Бишкеке в субботу?"

#: Операторский чат для тестов. Отрицательный id — как у настоящей группы.
OPERATOR_CHAT = -5500000001


@pytest.fixture(autouse=True)
def operator_chat_is_not_configured(monkeypatch):
    """По умолчанию передача оператору не настроена.

    Иначе тесты зависели бы от машины: в рабочем `.env` у `OPERATOR_CHAT_ID`
    есть значение, и бот начал бы пересылать обращения в настоящую группу
    прямо из прогона. Тест, которому пересылка нужна, включает её сам
    (:func:`_operator_chat`).
    """
    monkeypatch.delenv(handoff.OPERATOR_CHAT_ENV, raising=False)
    monkeypatch.setattr(handoff, "ENV_FILE",
                        Path(__file__).with_name("нет-такого.env"))


def _operator_chat(monkeypatch, chat_id: int = OPERATOR_CHAT) -> str:
    """Включить передачу оператору в этот чат."""
    monkeypatch.setenv(handoff.OPERATOR_CHAT_ENV, str(chat_id))
    return str(chat_id)


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
# Дефект 2: ответ дозревает, а не ждёт тишины
# --------------------------------------------------------------------------

class _FakeSent:
    """Сообщение, которое бот уже отправил: его можно переписать."""

    def __init__(self, text: str, markup: object = None,
                 error: list[BaseException] | None = None) -> None:
        self.text = text
        self.markup = markup
        self.edits: list[tuple[str, object]] = []
        #: Список, в который тест кладёт ошибку Telegram уже после
        #: отправки: именно так и выглядит живой случай «сообщение
        #: удалили, а потом пришла дописка».
        self._error = error if error is not None else []

    async def edit_text(self, text: str, **kwargs: object) -> None:
        if self._error:
            raise self._error[0]
        self.text = text
        self.markup = kwargs.get("reply_markup")
        self.edits.append((text, kwargs.get("reply_markup")))


class _FakeMessage:
    """Сообщение Telegram в объёме, который нужен обработчику."""

    def __init__(self, text: str = "",
                 edit_error: list[BaseException] | None = None) -> None:
        self.text = text
        self.voice: object | None = None
        self.replies: list[_FakeSent] = []
        #: Чем Telegram ответит на попытку переписать отправленное.
        self.edit_error = edit_error if edit_error is not None else []

    async def reply_text(self, text: str, **kwargs: object) -> _FakeSent:
        sent = _FakeSent(text, kwargs.get("reply_markup"), self.edit_error)
        self.replies.append(sent)
        return sent


class _FakeUpdate:
    """Обновление Telegram: чат и сообщение, больше обработчику не нужно."""

    def __init__(self, chat_id: int, text: str = "",
                 edit_error: list[BaseException] | None = None) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = _FakeMessage(text, edit_error)


class _FakeVoice:
    """Голосовое вложение: длительность и скачивание файла."""

    def __init__(self, duration: int = 5, payload: bytes = b"opus" * 300,
                 folder: list[str] | None = None) -> None:
        self.duration = duration
        self.file_unique_id = "voice-1"
        self._payload = payload
        self.saved: list[str] = []
        self.left_on_disk = folder if folder is not None else []

    async def get_file(self) -> _FakeVoice:
        return self

    async def download_to_drive(self, custom_path: str) -> None:
        self.saved.append(custom_path)
        Path(custom_path).write_bytes(self._payload)


class _Dialog:
    """Переписка одного чата через настоящие обработчики бота.

    Считает ровно то, что видит абонент: сколько сообщений появилось в
    чате и сколько раз они переписаны.
    """

    def __init__(self, chat_id: int, clock: _Clock | None = None,
                 context: object | None = None) -> None:
        self.chat_id = chat_id
        self.clock = clock or _Clock()
        bot._flow = bot.ConversationFlow(clock=self.clock)
        self.sent: list[_FakeSent] = []
        #: Сюда тест кладёт ошибку, которой Telegram ответит на правку.
        self.edit_error: list[BaseException] = []
        #: Клиент Telegram нужен только пересылке оператору; обычным
        #: ответам он не требуется, поэтому по умолчанию его нет.
        self.context = context

    def says(self, text: str) -> _Dialog:
        """Абонент пишет реплику."""
        update = _FakeUpdate(self.chat_id, text, self.edit_error)
        asyncio.run(bot._on_text(update, self.context))
        self.sent.extend(update.effective_message.replies)
        return self

    def sends_voice(self, voice: _FakeVoice) -> _Dialog:
        """Абонент присылает голосовое."""
        update = _FakeUpdate(self.chat_id, "", self.edit_error)
        update.effective_message.voice = voice
        asyncio.run(bot._on_voice(update, self.context))
        self.sent.extend(update.effective_message.replies)
        return self

    @property
    def messages(self) -> list[str]:
        """Текст всех сообщений чата в их нынешнем виде."""
        return [sent.text for sent in self.sent]

    @property
    def last(self) -> str:
        """Последнее сообщение чата."""
        return self.sent[-1].text

    @property
    def edits(self) -> int:
        """Сколько правок своих сообщений сделал бот."""
        return sum(len(sent.edits) for sent in self.sent)


def test_first_message_is_answered_immediately():
    """Первая реплика получает ответ сразу, без окна ожидания.

    Прежний буфер ждал две секунды тишины: каждый одиночный вопрос
    задерживался на столько же.
    """
    pytest.importorskip("telegram")

    chat = _Dialog(80).says("Как проверить баланс?")

    assert len(chat.sent) == 1
    assert chat.edits == 0
    assert entry_by_id(1).answer_ru in chat.last


def test_a_long_kyrgyz_pause_is_still_one_thought():
    """Тот самый дефект: пауза внутри мысли больше прежних двух секунд.

    Живой тест: абонент печатал кыргызский текст с телефона, пауза между
    репликами одной мысли оказалась больше окна склейки, и бот вклинился
    на середине. Теперь ответ уже на экране и просто дозревает: второе
    сообщение в чат не приходит, прежнее переписывается.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(81, clock)

    chat.says("салам алейкум")
    clock.advance(4.0)
    chat.says("менин интернетим иштебей жатат")

    assert len(chat.sent) == 1
    assert chat.edits == 1
    assert entry_by_id(3).answer_ky in chat.last


def test_the_answer_is_recounted_on_the_joined_text():
    """Ответ пересчитывается по склеенному тексту, а не по последней части."""
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(82, clock)

    chat.says("крч проблема такая")
    clock.advance(3.5)
    chat.says("типа сигнал есть")
    clock.advance(3.5)
    chat.says("но инета нет")

    assert len(chat.sent) == 1
    assert chat.edits == 2
    assert entry_by_id(3).answer_ru in chat.last


def test_revision_is_marked_so_it_is_not_missed():
    """Правка сообщения выше незаметна, поэтому бот помечает её строкой."""
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(83, clock)

    chat.says("интернет")
    clock.advance(2.0)
    chat.says("не работает")

    assert "учёл ваше уточнение" in chat.last


def test_a_message_after_the_window_is_a_new_message():
    """Реплика после окна дозревания — это новый вопрос и новое сообщение."""
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(84, clock)

    chat.says("Как проверить баланс?")
    clock.advance(bot.conversation().revise_window + 1)
    chat.says("Как сменить тариф?")

    assert len(chat.sent) == 2
    assert chat.edits == 0
    assert entry_by_id(1).answer_ru in chat.messages[0]
    assert entry_by_id(5).answer_ru in chat.messages[1]


def test_the_revision_window_comes_from_the_rules():
    """Окна заданы конфигом и лежат в обещанных пределах."""
    limits = presentation.rules().limits

    assert 8.0 <= limits.revise_window_seconds <= 10.0
    assert 0 < limits.read_seconds < limits.revise_window_seconds
    assert bot.ConversationFlow().revise_window == limits.revise_window_seconds
    assert bot.ConversationFlow().read_seconds == limits.read_seconds


def test_revisions_are_limited_so_the_chat_does_not_freeze():
    """Бесконечно переписывать одно сообщение нельзя.

    Иначе чат, в который пишут без остановки, навсегда остался бы с
    одним сообщением, а ограничитель флуда ничего бы не считал.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(85, clock)
    limit = bot.conversation().max_revisions

    for _ in range(limit + 2):
        chat.says("интернет")
        clock.advance(1.0)

    assert len(chat.sent) == 2
    assert chat.edits == limit


def test_revisions_do_not_eat_the_flood_budget():
    """Дописка — не новая реплика: бюджет ограничителя она не тратит.

    Человек, пишущий мысль пятью сообщениями, иначе заглушил бы сам
    себя и остался бы без ответа вовсе.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    bot._throttle = bot.ThrottleGuard(limit=2, window=600)
    chat = _Dialog(86, clock)

    for part in ("у меня", "не работает", "мобильный", "интернет", "совсем"):
        chat.says(part)
        clock.advance(1.0)

    assert len(chat.sent) == 1
    assert "Слишком много сообщений" not in chat.last
    assert entry_by_id(3).answer_ru in chat.last


def test_a_revised_answer_stays_one_turn_in_the_dialog_memory():
    """Одна мысль — одна реплика в памяти диалога, сколько бы частей ни было.

    Иначе вопрос, набранный тремя сообщениями, вытеснил бы из короткой
    памяти всю историю чата и трижды продлил бы цепочку отказов.
    """
    pytest.importorskip("telegram")
    from faq_bot.answer import MEMORY

    clock = _Clock()
    chat = _Dialog(87, clock)

    chat.says("Как подключить роуминг?")
    clock.advance(2.0)
    chat.says("а сколько стоит")

    assert len(MEMORY.turns(87)) == 1
    assert MEMORY.recent_topic(87) == 4
    assert entry_by_id(4).answer_ru in chat.last


def test_three_unanswerable_parts_do_not_summon_the_operator():
    """Цепочка отказов считает мысли, а не сообщения.

    Три части одной непонятной мысли — это один отказ, а не три, и
    предлагать оператора на первой же мысли бот не должен.
    """
    pytest.importorskip("telegram")
    from faq_bot.answer import MEMORY

    clock = _Clock()
    chat = _Dialog(88, clock)

    chat.says("какая погода")
    clock.advance(1.0)
    chat.says("будет в бишкеке")
    clock.advance(1.0)
    chat.says("в субботу")

    assert MEMORY.misses(88) == 1
    assert "не понимаю вопрос" not in chat.last


def test_failed_edit_falls_back_to_a_new_message():
    """Переписать нечего (сообщение удалили) — отвечаем новым, а не молчим."""
    telegram = pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(89, clock)
    chat.says("интернет")
    chat.edit_error.append(
        telegram.error.BadRequest("Message to edit not found"))

    clock.advance(1.0)
    chat.says("не работает")

    assert len(chat.sent) == 2
    assert entry_by_id(3).answer_ru in chat.last


def test_pressing_a_button_closes_the_revision_window():
    """Нажали кнопку — ответ прочитан, дописывать к нему нечего."""
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(90, clock)
    chat.says("Как проверить баланс?")

    _press(90, "menu")
    clock.advance(1.0)
    chat.says("Как сменить тариф?")

    assert len(chat.sent) == 2
    assert entry_by_id(5).answer_ru in chat.last


def test_the_flow_does_not_mix_chats():
    """Реплики разных чатов не склеиваются между собой."""
    pytest.importorskip("telegram")
    clock = _Clock()
    first = _Dialog(91, clock)
    second = _Dialog(92, clock)      # тот же процесс, другой chat_id

    first.says("не работает интернет")
    second.says("как сменить тариф")

    assert entry_by_id(3).answer_ru in first.last
    assert entry_by_id(5).answer_ru in second.last
    assert first.edits == 0 and second.edits == 0


def test_pasted_text_split_by_telegram_does_not_flood_the_chat():
    """Живой тест: Telegram разрезал вставленный текст на семь частей.

    Части дозревают в одном сообщении, склеенный текст оказывается
    длиннее порога — и абонент видит одну вежливую просьбу написать
    короче вместо семи красных карточек. Седьмая часть упирается в
    предел числа правок, поэтому сообщений не одно, а два — но не семь.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(93, clock)
    part = "Здравствуйте, расскажу свою ситуацию по порядку. " * 4

    for _ in range(7):
        chat.says(part)
        clock.advance(0.5)

    assert len(chat.sent) <= 2
    assert "слишком длинное" in chat.last


def test_throttled_chat_gets_one_warning_and_then_silence():
    """Заглушённый троттлингом чат получает предупреждение и тишину."""
    pytest.importorskip("telegram")
    clock = _Clock()
    bot._throttle = bot.ThrottleGuard(limit=1, window=600)
    chat = _Dialog(94, clock)

    chat.says("Как проверить баланс?")
    clock.advance(30.0)
    chat.says("Как пополнить счёт?")
    clock.advance(30.0)
    chat.says("Как сменить тариф?")

    assert len(chat.sent) == 2
    assert "Слишком много сообщений" in chat.messages[1]


def test_merging_does_not_depend_on_job_queue():
    """Дозревание живёт на правке сообщений, а не на планировщике.

    В установленной версии python-telegram-bot `application.job_queue`
    равен None: планировщик живёт в отдельном дополнении
    (`pip install "python-telegram-bot[job-queue]"`). Прежняя склейка
    держалась на отложенной задаче; теперь отложенного нет вовсе —
    ответ уходит сразу и правится на месте.
    """
    pytest.importorskip("telegram")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        application = bot.build_application(
            "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        assert application.job_queue is None

    clock = _Clock()
    chat = _Dialog(95, clock)
    chat.says("не работает")
    clock.advance(1.0)
    chat.says("интернет")

    assert len(chat.sent) == 1
    assert chat.edits == 1


# --------------------------------------------------------------------------
# Дефект 1: «этот совет не помог» — повторять ответ нельзя
# --------------------------------------------------------------------------

def test_live_kyrgyz_refusal_does_not_repeat_the_same_answer():
    """Дословный разбор живого теста на кыргызском.

    23:32 «салам алейкум. менин интернетим чето жаман иштеп жатат»
          — карточка «Эмне үчүн интернет иштебейт?»
    23:33 «гб бар экен, бирок баары бир иштебей жатат»
          — и бот повторил ту же карточку слово в слово.

    Теперь второй реплики с тем же текстом не бывает: бот признаёт, что
    совет не помог, и показывает другие темы.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(100, clock)

    chat.says("салам алейкум. менин интернетим чето жаман иштеп жатат")
    first = chat.last
    clock.advance(60.0)
    chat.says("гб бар экен, бирок баары бир иштебей жатат")

    assert entry_by_id(3).answer_ky in first
    assert entry_by_id(3).answer_ky not in chat.last
    assert "жардам бербеди" in chat.last
    assert len(chat.sent) == 2


def test_refusal_offers_other_topics_and_an_operator():
    """На отказ — другие темы кнопками и живой оператор."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 101)
    clock.advance(60.0)
    screen = bot.screen_for_text("не помогло", 101)
    actions = screen.actions()
    topics = [action for action in actions if action.startswith("faq:")]

    assert "operator" in actions
    assert 1 <= len(topics) <= config.TOP_K
    assert "faq:3" not in topics            # то, что не помогло, не предлагаем
    assert "faq:10" in topics               # APN — следующий вопрос оператора


def test_second_refusal_hands_the_dialog_to_a_human():
    """Третье касание той же темы — сразу оператор, без новых советов."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 102)
    clock.advance(30.0)
    bot.screen_for_text("не помогло", 102)
    clock.advance(30.0)
    screen = bot.screen_for_text("всё равно не работает", 102)
    rendered = _text(screen)

    assert presentation.plain_dashes(
        presentation.operator_facts(entry_by_id(14), "ru")) in rendered
    assert "Напишите «оператор»" not in rendered
    assert "Совет не помог" in rendered
    assert "Больше советов по этой теме" in rendered
    reasons = dict(stats_snapshot().operator_reasons)
    assert reasons.get("not_helping") == 1


def test_the_same_topic_twice_in_a_row_is_a_refusal_without_words():
    """Повтор темы — сигнал сам по себе, даже без слов отказа.

    Живой оператор не зачитывает одну и ту же инструкцию дважды.
    """
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    first = _text(bot.screen_for_text("почему не работает интернет", 103))
    clock.advance(40.0)
    second = _text(bot.screen_for_text("интернет не работает у меня", 103))

    assert entry_by_id(3).answer_ru in first
    assert entry_by_id(3).answer_ru not in second
    assert "жардам" not in second            # и не на чужом языке
    assert "Может быть, дело в этом" in second


def test_the_same_topic_after_the_window_is_answered_again():
    """Через окно повтора тот же вопрос снова получает обычный ответ.

    Иначе абонент, которому утром не помог совет про интернет, вечером
    не получил бы ответа про интернет вообще.
    """
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 104)
    clock.advance(bot.conversation().repeat_window + 10)
    rendered = _text(bot.screen_for_text("почему не работает интернет", 104))

    assert entry_by_id(3).answer_ru in rendered


def test_a_topic_chosen_by_button_twice_is_not_a_refusal():
    """Абонент сам нажал тему — повторять его выбор не ошибка бота."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_action("faq:3", 105)
    clock.advance(20.0)
    rendered = _text(bot.screen_for_text("почему не работает интернет", 105))

    assert entry_by_id(3).answer_ru in rendered


def test_refusal_words_with_a_new_question_answer_the_new_question():
    """«Не помогло, а как пополнить счёт?» — это новый вопрос, а не отказ.

    Слишком жадное распознавание отказа испортило бы обычные вопросы,
    которых в проверке в восемь раз больше, чем диалогов.
    """
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 106)
    clock.advance(30.0)
    rendered = _text(bot.screen_for_text("не помогло, а как пополнить счёт?",
                                         106))

    assert entry_by_id(11).answer_ru in rendered


def test_refusal_words_without_a_previous_answer_are_just_a_question():
    """Первой репликой «не работает» — обычный вопрос: отвергать нечего."""
    bot._flow = bot.ConversationFlow()

    rendered = _text(bot.screen_for_text("интернет не работает", 107))

    assert entry_by_id(3).answer_ru in rendered


def test_a_refusal_while_the_answer_is_unread_is_a_revision():
    """Граница «дописка или отказ»: ответ ещё не успели прочитать.

    Абонент дописывает «баары бир иштебей жатат» к своему же вопросу —
    он печатал, когда ответ появился. Это уточнение: ответ
    пересчитывается и переписывает прежний.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(108, clock)

    chat.says("интернетим иштебейт")
    clock.advance(1.0)                       # меньше времени на чтение
    chat.says("баары бир иштебей жатат")

    assert len(chat.sent) == 1               # переписали, а не ответили
    assert chat.edits == 1
    assert "жардам бербеди" not in chat.last


def test_a_refusal_after_reading_is_a_refusal():
    """Та же фраза после прочтения ответа — уже отказ, а не дописка."""
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(109, clock)

    chat.says("интернетим иштебейт")
    clock.advance(bot.conversation().read_seconds + 1.0)
    chat.says("баары бир иштебей жатат")

    assert len(chat.sent) == 2               # новая реплика, новый ответ
    assert "жардам бербеди" in chat.last


def test_ack_closes_the_refusal_chain():
    """«Понятно» означает «совет подошёл»: цепочка отказов забыта."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 110)
    clock.advance(30.0)
    bot.screen_for_text("не помогло", 110)
    bot.screen_for_action("ack:10", 110)
    clock.advance(10.0)
    rendered = _text(bot.screen_for_text("почему не работает интернет", 110))

    assert entry_by_id(3).answer_ru in rendered


def test_a_rejected_topic_is_not_offered_again():
    """Тему, про которую сказали «не помогло», бот второй раз не советует."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 111)
    clock.advance(30.0)
    screen = bot.screen_for_text("не помогло", 111)
    offered = [action for action in screen.actions()
               if action.startswith("faq:")]

    assert bot.conversation().rejected(111) == (3,)
    assert "faq:3" not in offered


def test_refusal_speaks_the_language_of_the_chat():
    """Экран отказа печатается на одном языке, как и всё остальное."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("интернетим иштебейт", 112)
    clock.advance(30.0)
    rendered = _text(bot.screen_for_text("баары бир иштебей жатат", 112))

    assert "Балким, себеби ушунда" in rendered
    assert "Может быть" not in rendered


def test_refusal_state_does_not_leak_between_chats():
    """Отказы одного чата не видны другому."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("почему не работает интернет", 113)
    clock.advance(30.0)
    bot.screen_for_text("не помогло", 113)
    rendered = _text(bot.screen_for_text("почему не работает интернет", 114))

    assert bot.conversation().rejected(114) == ()
    assert entry_by_id(3).answer_ru in rendered


def test_refusal_words_are_configured_in_the_rules():
    """Слова отказа лежат в bot_rules.json, а не в коде."""
    phrases = presentation.rules().refusal.phrases

    assert "баары бир" in phrases
    assert "не помогло" in phrases
    assert presentation.says_refusal("гб бар экен, бирок баары бир иштебей")
    assert presentation.says_refusal("уже проверил, всё равно нет")
    assert not presentation.says_refusal("Как проверить баланс?")
    assert not presentation.says_refusal("менин интернетим жаман иштеп жатат")


# --------------------------------------------------------------------------
# Голосовые сообщения
# --------------------------------------------------------------------------

def _fake_speech(monkeypatch, status: str = "ok", text: str = "",
                 seen: list[dict] | None = None):
    """Подменить распознавание. Настоящий API в тестах не вызывается:
    ключ оплачивает человек, а в CI его нет вовсе."""
    from faq_bot import speech

    def transcribe(path, *, chat_id=None, declared_seconds=0.0,
                   model=speech.DEFAULT_MODEL):
        if seen is not None:
            seen.append({"path": str(path),
                         "exists": Path(path).exists(),
                         "chat_id": chat_id,
                         "declared_seconds": declared_seconds})
        return speech.Transcript(status, text=text, seconds=declared_seconds)

    monkeypatch.setattr(speech, "transcribe", transcribe)
    return speech


def test_voice_is_answered_through_the_usual_path(monkeypatch):
    """Распознанный текст идёт тем же путём, что набранный руками."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, "ok", "балансты кантип текшерем")
    chat = _Dialog(120)

    chat.sends_voice(_FakeVoice(duration=4))

    assert len(chat.sent) == 1               # одно сообщение, как на текст
    assert entry_by_id(1).answer_ky in chat.last


def test_voice_shows_what_was_heard(monkeypatch):
    """Эхо услышанного: иначе непонятно, почему ответ мимо."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, "ok", "как пополнить счёт")
    chat = _Dialog(121)

    chat.sends_voice(_FakeVoice())

    assert "🎤" in chat.last
    assert "как пополнить счёт" in chat.last
    assert entry_by_id(11).answer_ru in chat.last


def test_voice_message_is_edited_not_doubled(monkeypatch):
    """Сообщение «слушаю запись» переписывается ответом, а не дополняется."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, "ok", "как проверить баланс")
    chat = _Dialog(122)

    chat.sends_voice(_FakeVoice())

    assert len(chat.sent) == 1
    assert chat.edits == 1
    assert "Слушаю запись" not in chat.last


@pytest.mark.parametrize("status, expected", [
    ("too_long", "слишком длинная"),
    ("rate_limited", "слишком много"),
    ("no_key", "недоступно"),
    ("nothing", "Не расслышал"),
    ("failed", "Не расслышал"),
])
def test_every_voice_failure_gets_an_honest_answer(monkeypatch, status,
                                                   expected):
    """Каждый статус распознавания объяснён абоненту, и на двух языках."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, status)
    chat = _Dialog(123)

    chat.sends_voice(_FakeVoice(duration=90))

    assert expected in chat.last
    assert len(chat.sent) == 1


def test_voice_texts_exist_in_both_languages():
    """Тексты про голос живут в конфиге и переведены оба."""
    messages = presentation.rules().messages
    names = ["voice_listening", "voice_too_long", "voice_rate_limited",
             "voice_no_key", "voice_nothing"]

    for name in names:
        for language in config.LANGUAGES:
            assert messages[name][language], (name, language)
    assert set(bot.VOICE_NOTICES.values()) <= set(names)


def test_voice_file_is_deleted_after_recognition(monkeypatch):
    """Голос абонента не остаётся на диске сервера."""
    pytest.importorskip("telegram")
    seen: list[dict] = []
    _fake_speech(monkeypatch, "ok", "как проверить баланс", seen)
    voice = _FakeVoice(duration=7)

    _Dialog(124).sends_voice(voice)

    assert len(seen) == 1
    assert seen[0]["exists"]                      # файл был на месте
    assert seen[0]["declared_seconds"] == 7       # длительность — из Telegram
    assert seen[0]["chat_id"] == "124"
    assert not Path(seen[0]["path"]).exists()     # и удалён после
    assert not Path(voice.saved[0]).parent.exists()


def test_voice_survives_a_download_failure(monkeypatch):
    """Упавшая загрузка не роняет бота и не молчит."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, "ok", "как проверить баланс")

    class _Broken(_FakeVoice):
        async def get_file(self):
            raise RuntimeError("сеть")

    chat = _Dialog(125).sends_voice(_Broken())

    assert "Не расслышал" in chat.last


def test_voice_obeys_the_throttle_before_spending_money(monkeypatch):
    """Ограничитель срабатывает ДО распознавания: оно платное.

    Проверка идёт по числу обращений к распознаванию, а не по тексту
    ответа: заглушённый чат не должен стоить владельцу ключа ни секунды.
    """
    pytest.importorskip("telegram")
    seen: list[dict] = []
    _fake_speech(monkeypatch, "ok", "как проверить баланс", seen)
    bot._throttle = bot.ThrottleGuard(limit=1, window=600)
    chat = _Dialog(126)

    chat.sends_voice(_FakeVoice())
    chat.sends_voice(_FakeVoice())
    chat.sends_voice(_FakeVoice())

    assert len(seen) == 1
    assert "Слишком много сообщений" in chat.messages[1]
    assert len(chat.sent) == 2                    # третье — молчание


def test_voice_does_not_count_twice_against_the_throttle(monkeypatch):
    """Голосовое тратит один ответ бюджета, а не два."""
    pytest.importorskip("telegram")
    _fake_speech(monkeypatch, "ok", "как проверить баланс")
    bot._throttle = bot.ThrottleGuard(limit=2, window=600)
    chat = _Dialog(127)

    chat.sends_voice(_FakeVoice())
    chat.sends_voice(_FakeVoice())

    assert len(chat.sent) == 2
    assert all("Слишком много" not in text for text in chat.messages)


def test_voice_limits_are_not_weakened():
    """Защиты от трат остались такими, какими их написал speech.py."""
    from faq_bot import speech

    assert speech.MAX_AUDIO_SECONDS == 60.0
    assert speech.BUDGET_PER_CHAT_HOUR == 180.0
    assert speech.BUDGET_TOTAL_HOUR == 900.0
    assert speech.BUDGET_TOTAL_DAY == 3600.0
    assert speech.DEFAULT_MODEL.endswith("flash-lite-latest")


def test_speech_is_imported_lazily(monkeypatch):
    """Модуль распознавания не нужен ни `answer()`, ни импорту бота.

    `monkeypatch.delitem` вместо `sys.modules.pop`: иначе в процессе
    осталось бы два экземпляра пакета, и соседние тесты сбрасывали бы
    статистику не того.
    """
    for name in [item for item in sys.modules
                 if item.startswith(("faq_bot", "telegram"))]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    importlib.import_module("faq_bot.bot")

    assert "faq_bot.speech" not in sys.modules


def test_voice_handler_is_registered():
    """Голосовые действительно подключены к приложению Telegram."""
    telegram = pytest.importorskip("telegram")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        application = bot.build_application(
            "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    handlers = application.handlers[0]
    voice = [item for item in handlers
             if getattr(item, "callback", None) is bot._on_voice]
    assert len(voice) == 1
    assert isinstance(voice[0], telegram.ext.MessageHandler)


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

    for text in ("баланс", "пакет", "тариф"):
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
    """Бонус ТЗ на месте: слово «оператор» ведёт к живому человеку.

    И не цитирует при этом инструкцию «напишите „оператор“ в боте» —
    человек её только что выполнил. Факт из базы (часы работы) остаётся.
    """
    rendered = _text(bot.screen_for_text("оператор", 41))

    assert "Попросили оператора" in rendered
    assert presentation.plain_dashes(
        presentation.operator_facts(entry_by_id(14), "ru")) in rendered
    assert "Напишите «оператор»" not in rendered


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


# --------------------------------------------------------------------------
# Диалог из ТЗ не должен пострадать от распознавания отказа
# --------------------------------------------------------------------------

def test_a_clarifying_question_about_the_same_topic_is_answered():
    """Пункт 4 ТЗ сильнее признака повтора.

    «А сколько стоит?» после вопроса о роуминге — уточнение по той же
    теме, и абонент обязан получить ответ, а не экран «совет не помог».
    Пауза заведомо больше окна дозревания: человек прочитал ответ и
    только потом спросил про цену.
    """
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("Как подключить роуминг?", 130)
    clock.advance(30.0)
    rendered = _text(bot.screen_for_text("А сколько стоит?", 130))

    assert entry_by_id(4).answer_ru in rendered
    assert "не помог" not in rendered


def test_the_whole_dialog_example_works_through_the_bot_layer():
    """Образец организаторов проходит целиком и через слой бота.

    `answer()` его и так проходит, но демонстрируют на защите бота:
    экран обязан показать ответ темы 4 на обе реплики.
    """
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)
    dialogs = json.loads(
        config.DIALOGS_PATH.read_text(encoding="utf-8"))

    for dialog in dialogs:
        for number, turn in enumerate(dialog["turns"]):
            clock.advance(30.0)
            rendered = _text(bot.screen_for_text(turn["text"], 131))
            expected = entry_by_id(turn["faq_id"])
            assert expected.answer(turn["lang"]) in rendered, (
                dialog["id"], number, rendered)


def test_a_refusal_still_wins_over_an_inherited_topic():
    """А вот явные слова отказа сильнее наследования темы."""
    clock = _Clock()
    bot._flow = bot.ConversationFlow(clock=clock)

    bot.screen_for_text("Как подключить роуминг?", 132)
    clock.advance(30.0)
    rendered = _text(bot.screen_for_text("не помогло", 132))

    assert entry_by_id(4).answer_ru not in rendered
    assert "совет не помог" in rendered.lower()


def test_a_long_thought_in_four_parts_stays_one_message():
    """Окно отсчитывается от последней правки, а не от первого ответа.

    Кыргызский текст с телефона набирается медленно: четыре части по
    четыре секунды — это пятнадцать секунд одной мысли. При окне,
    привязанном к первому ответу, мысль разорвалась бы на середине, то
    есть дефект вернулся бы, только растянутый.
    """
    pytest.importorskip("telegram")
    clock = _Clock()
    chat = _Dialog(133, clock)

    chat.says("салам")
    for part in ("менин телефонумда", "интернет жок", "эмне кылсам болот"):
        clock.advance(4.0)
        chat.says(part)

    assert len(chat.sent) == 1
    assert chat.edits == 3
    assert entry_by_id(3).answer_ky in chat.last


# --------------------------------------------------------------------------
# Передача оператору живому человеку: карточка в его чат и реплай обратно
# --------------------------------------------------------------------------

class _Posted:
    """Сообщение, которое бот отправил не в ответ, а в другой чат."""

    def __init__(self, message_id: int, chat_id: object, text: str,
                 markup: object = None) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        self.markup = markup


class _FakeBot:
    """Клиент Telegram в объёме, нужном пересылке: один `send_message`."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.posted: list[_Posted] = []
        self._error = error
        self._next_id = 900

    async def send_message(self, chat_id: object, text: str,
                           **kwargs: object) -> _Posted:
        if self._error is not None:
            raise self._error
        self._next_id += 1
        sent = _Posted(self._next_id, chat_id, text,
                       kwargs.get("reply_markup"))
        self.posted.append(sent)
        return sent

    def to(self, chat_id: object) -> list[_Posted]:
        """Что ушло в этот чат."""
        return [item for item in self.posted
                if str(item.chat_id) == str(chat_id)]


class _FakeContext:
    """`context` обработчика: из него пересылке нужен только `bot`."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.bot = _FakeBot(error)


class _OperatorUpdate:
    """Сообщение в операторском чате, с реплаем или без."""

    def __init__(self, text: str, reply_to: int | None = None,
                 chat_id: int = OPERATOR_CHAT, voice: object = None) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id,
                                                "username": None})()
        self.effective_message = _FakeMessage(text)
        self.effective_message.voice = voice
        self.effective_message.reply_to_message = (
            None if reply_to is None
            else type("Replied", (), {"message_id": reply_to})())


def _operator_says(text: str, reply_to: int | None = None,
                   context: _FakeContext | None = None,
                   voice: object = None) -> _OperatorUpdate:
    """Оператор пишет в своём чате через настоящий обработчик."""
    update = _OperatorUpdate(text, reply_to, voice=voice)
    asyncio.run(bot._on_operator_message(update, context or _FakeContext()))
    return update


def _ask_with_operator(monkeypatch, chat_id: int,
                       text: str) -> tuple[_Dialog, _FakeBot]:
    """Абонент пишет в чат, где передача оператору настроена."""
    _operator_chat(monkeypatch)
    context = _FakeContext()
    chat = _Dialog(chat_id, context=context)
    chat.says(text)
    return chat, context.bot


def test_without_an_operator_chat_the_bot_forwards_nothing():
    """Переменной нет — всё работает как прежде: ни падений, ни ошибок."""
    pytest.importorskip("telegram")
    context = _FakeContext()
    chat = _Dialog(200, context=context)

    chat.says("оператор")

    assert not handoff.enabled()
    assert context.bot.posted == []
    assert "Попросили оператора" in chat.last
    assert "🎫" not in chat.last
    assert len(chat.sent) == 1


def test_handoff_sends_a_card_to_the_operator_chat(monkeypatch):
    """Обращение уходит карточкой в операторский чат."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    chat = _Dialog(201, context=context)

    chat.says("почему не работает интернет")
    chat.clock.advance(60.0)
    chat.says("оператор")

    cards = context.bot.to(target)
    assert len(cards) == 1
    card = cards[0]
    assert "Обращение № 0001" in card.text
    assert "Попросили оператора" in card.text          # причина с подписью
    assert "Русский" in card.text                      # язык абонента
    assert "почему не работает интернет" in card.text  # текст вопроса
    assert "тема 3" in card.text                       # что ответил бот
    assert "уверенность" in card.text
    assert "реплаем" in card.text                      # как отвечать


def test_the_card_carries_no_personal_data(monkeypatch):
    """В карточке нет ни имени, ни username, ни chat_id абонента.

    Для ответа по существу они не нужны: доставку обеспечивает реплай,
    а чат абонента помнит бот. Уточнения организаторов требуют не
    раскрывать чужие персональные данные — не собирать лишнего это то же
    правило, только на шаг раньше.
    """
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    update = _FakeUpdate(2024771, "хочу написать жалобу")
    update.effective_user = type("User", (), {"first_name": "Айбек",
                                              "username": "aibek_kg",
                                              "id": 2024771})()

    asyncio.run(bot._on_text(update, context))
    card = context.bot.to(target)[0].text

    assert "Айбек" not in card
    assert "aibek_kg" not in card
    assert "2024771" not in card
    assert "Жалоба или претензия" in card


def test_the_subscriber_learns_that_the_request_is_accepted(monkeypatch):
    """Абонент видит номер обращения и что ответ придёт сюда же."""
    pytest.importorskip("telegram")

    chat, _ = _ask_with_operator(monkeypatch, 202, "оператор")

    assert len(chat.sent) == 1                  # правка, а не второе письмо
    assert chat.edits == 1
    assert "🎫" in chat.last
    assert "0001" in chat.last
    assert "ответ придёт в этот же чат" in chat.last


def test_a_promise_is_not_given_when_the_card_did_not_reach(monkeypatch):
    """Карточка не ушла — ответа оператора абоненту не обещаем."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext(RuntimeError("чат не найден"))
    chat = _Dialog(203, context=context)

    chat.says("оператор")

    assert "🎫" not in chat.last
    assert "ответ придёт" not in chat.last
    assert "Попросили оператора" in chat.last   # сам ответ бота на месте


def test_operator_reply_reaches_exactly_that_subscriber(monkeypatch):
    """Реплай оператора доставляется тому абоненту, чья это карточка."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(204, context=context).says("оператор")
    _Dialog(205, context=context).says("оператор")
    cards = context.bot.to(target)

    _operator_says("Проверил вашу линию, авария устранена.",
                   reply_to=cards[0].message_id, context=context)

    delivered = context.bot.to(204)
    assert len(delivered) == 1
    assert "Ответ оператора" in delivered[0].text
    assert "Проверил вашу линию" in delivered[0].text
    assert context.bot.to(205) == []            # чужому абоненту — ничего


def test_operator_gets_a_receipt_for_a_delivered_answer(monkeypatch):
    """Оператор видит расписку: его текст доставлен."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(206, context=context).says("оператор")
    card = context.bot.to(target)[0]

    update = _operator_says("Уже включили.", reply_to=card.message_id,
                            context=context)

    assert len(update.effective_message.replies) == 1
    assert "доставлен" in update.effective_message.replies[0].text
    assert "0001" in update.effective_message.replies[0].text


def test_operator_answer_is_delivered_in_the_language_of_the_subscriber(
        monkeypatch):
    """Пометка ответа печатается на языке абонента, а не оператора."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(207, context=context).says("менин интернетим иштебейт, оператор")
    card = context.bot.to(target)[0]

    _operator_says("Текшердик, азыр иштейт.", reply_to=card.message_id,
                   context=context)

    assert "Кыргызча" in card.text
    assert "Оператордун жообу" in context.bot.to(207)[0].text


def test_a_reply_to_an_unknown_card_gets_an_explanation(monkeypatch):
    """Бот перезапускался и карточку не помнит — говорим об этом прямо."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()

    update = _operator_says("Перезвоните абоненту", reply_to=777,
                            context=context)

    assert context.bot.posted == []             # абоненту ничего не ушло
    assert len(update.effective_message.replies) == 1
    assert "перезапускался" in update.effective_message.replies[0].text


def test_a_message_from_the_operator_chat_is_not_a_question(monkeypatch):
    """Сообщение из операторского чата не идёт обычным путём поиска."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()
    update = _FakeUpdate(OPERATOR_CHAT, "как проверить баланс")

    asyncio.run(bot._on_text(update, context))

    assert update.effective_message.replies == []
    assert context.bot.posted == []
    assert stats_snapshot().total == 0


def test_an_operator_message_without_a_reply_is_left_alone(monkeypatch):
    """Переписка операторов между собой бота не касается."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()

    update = _operator_says("коллеги, я на обеде", context=context)

    assert update.effective_message.replies == []
    assert context.bot.posted == []


def test_a_voice_from_the_operator_is_not_processed(monkeypatch):
    """Голосовые от оператора не обрабатываются — ни текстом, ни деньгами."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(208, context=context).says("оператор")
    card = context.bot.to(str(OPERATOR_CHAT))[0]
    before = len(context.bot.posted)

    update = _operator_says("", reply_to=card.message_id, context=context,
                            voice=_FakeVoice())
    voice_update = _FakeUpdate(OPERATOR_CHAT, "")
    voice_update.effective_message.voice = _FakeVoice()
    asyncio.run(bot._on_voice(voice_update, context))

    assert update.effective_message.replies == []
    assert voice_update.effective_message.replies == []
    assert len(context.bot.posted) == before


def test_the_desk_keeps_the_link_in_memory_only(monkeypatch):
    """Связь «карточка ↔ абонент» живёт в памяти процесса, без БД."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(209, context=context).says("оператор")
    card = context.bot.to(target)[0]

    found = handoff.DESK.find(card.message_id)
    handoff.DESK.clear()

    assert found is not None
    assert found.chat_id == "209"
    assert handoff.DESK.find(card.message_id) is None


def test_the_button_hands_over_the_last_question(monkeypatch):
    """Кнопка «0 Оператор» своего текста не имеет — везём последний вопрос."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    chat = _Dialog(210, context=context)
    chat.says("почему не работает интернет")

    query = _FakeQuery("operator")
    asyncio.run(bot._on_button(_FakeButtonUpdate(210, query), context))

    assert "почему не работает интернет" in context.bot.to(target)[0].text
    assert "🎫" in query.edits[-1][0]
    assert "0001" in query.edits[-1][0]
    assert query.message.replies == []          # и ни одного нового письма


def test_every_handoff_counted_in_stats_reaches_the_operator(monkeypatch):
    """Что попало в /stats как переданное, то и ушло человеку."""
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()

    _Dialog(211, context=context).says("оператор")
    _Dialog(212, context=context).says("верните деньги немедленно")
    chat = _Dialog(213, context=context)
    for _ in range(3):
        chat.clock.advance(60.0)
        chat.says(OUT_OF_SCOPE)

    reasons = dict(stats_snapshot().operator_reasons)
    assert reasons == {"direct_request": 1, "refund": 1, "repeated_miss": 1}
    assert len(context.bot.to(target)) == sum(reasons.values())


def test_the_operator_handler_is_registered_only_when_configured(monkeypatch):
    """Обработчик операторского чата подключается, только если он задан."""
    telegram = pytest.importorskip("telegram")
    import warnings

    def handlers() -> list:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            application = bot.build_application(
                "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        return [item for item in application.handlers[0]
                if getattr(item, "callback", None) is bot._on_operator_message]

    assert handlers() == []
    _operator_chat(monkeypatch)
    registered = handlers()

    assert len(registered) == 1
    assert isinstance(registered[0], telegram.ext.MessageHandler)


def test_the_operator_chat_is_read_from_the_env_file(monkeypatch, tmp_path):
    """`OPERATOR_CHAT_ID` читается так же, как токен: окружение или .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("# комментарий\nOPERATOR_CHAT_ID=-100500\n",
                        encoding="utf-8")
    monkeypatch.setattr(handoff, "ENV_FILE", env_file)

    assert handoff.operator_chat() == "-100500"
    assert handoff.is_operator_chat(-100500)
    assert not handoff.is_operator_chat(777)
    monkeypatch.setenv(handoff.OPERATOR_CHAT_ENV, "-42")
    assert handoff.operator_chat() == "-42"       # окружение главнее файла


def test_a_broken_env_file_is_not_a_crash(monkeypatch, tmp_path):
    """Нет файла или он нечитаем — пустая строка, а не трассировка."""
    monkeypatch.setattr(handoff, "ENV_FILE", tmp_path / "нет-такого")

    assert handoff.operator_chat() == ""
    assert not handoff.enabled()
    assert not handoff.is_operator_chat(OPERATOR_CHAT)
    assert handoff.DESK.remember(1, "direct_request") is None


def test_the_desk_does_not_grow_without_a_limit():
    """Память карточек ограничена: долгий прогон не съедает её целиком."""
    desk = handoff.HandoffDesk(limit=2)
    tickets = [handoff.Ticket(f"{number:04d}", str(number), "ru",
                              "direct_request", "вопрос")
               for number in range(3)]
    for number, ticket in enumerate(tickets):
        desk.bind(number, ticket)

    assert desk.find(0) is None                  # самая старая вытеснена
    assert desk.find(1) is tickets[1]
    assert desk.find(2) is tickets[2]


def test_a_bare_request_for_a_human_is_honest_in_the_card(monkeypatch):
    """Первой же репликой «оператор»: вопроса не было — так и написано.

    «Бот ответа не нашёл» в этом месте означало бы неудачу поиска,
    которой не случилось: искать было нечего.
    """
    pytest.importorskip("telegram")
    target = _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(214, context=context).says("оператор")

    card = context.bot.to(target)[0].text

    assert "своего вопроса нет" in card
    assert "ответа не нашёл" not in card
    assert "Попросили оператора" in card


# --------------------------------------------------------------------------
# Переформулировка ответа моделью: бонус «LLM с RAG по FAQ»
# --------------------------------------------------------------------------
#
# Главное требование к этому слою не «ответ стал красивее», а «абонент
# ничего не ждёт и ничего не теряет». Поэтому тесты ниже проверяют
# порядок событий и неизменность фактов, а не качество формулировки.

#: Честная переформулировка темы 1 под разговорный вопрос: код тот же,
#: новых цифр нет, длина не выросла, язык русский.
GOOD = "На счету смотрите через *100#, это бесплатно."

#: Нечестная: код настоящий, но принадлежит другой теме. Ровно случай
#: D-09 — абонент наберёт рабочую команду и получит не то.
FOREIGN_CODE = "Баланс смотрите через *111#, это бесплатно."

#: Выдуманная цифра, которой нет в записи базы.
INVENTED_NUMBER = "Наберите *100#, проверка бесплатна 3 раза в сутки."


class _FakeModel:
    """Транспорт переформулировки: отдаёт заготовленный текст."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, url: str, body: dict, timeout: float = 0.0,
                 *, api_key: str = "") -> dict:
        self.calls.append(body)
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


@pytest.fixture
def model(monkeypatch):
    """Переформулировка включена, сеть подменена. Возвращает установщик."""
    from faq_bot import rephrase

    rephrase.reset()
    monkeypatch.setattr(rephrase, "ENABLED", True)
    monkeypatch.setattr(rephrase, "api_key", lambda: "ключ-для-теста")

    def install(*replies: str) -> _FakeModel:
        fake = _FakeModel(*replies)
        monkeypatch.setattr(rephrase, "transport", fake)
        return fake

    yield install
    rephrase.reset()


def test_base_answer_is_shown_first_and_then_rewritten(model):
    """Абонент получает ответ базы сразу, формулировку модели — правкой.

    Это главное свойство всей затеи: сетевой вызов не задерживает показ
    готового ответа. В чате обязано остаться ОДНО сообщение, а не два.
    """
    fake = model(GOOD)
    chat = _Dialog(9100)

    chat.says("скока денег на счету")

    assert len(chat.sent) == 1, "переформулировка не смеет плодить сообщения"
    sent = chat.sent[0]
    # Первым в чате появился дословный текст базы.
    assert "*100#. Проверка бесплатная." in sent.edits[0][0] or \
           "*100#" in sent.edits[0][0]
    # И ровно одна правка — ею пришла формулировка модели.
    assert len(sent.edits) == 1
    assert "смотрите через *100#" in chat.last
    assert len(fake.calls) == 1


def test_only_the_body_changes(model):
    """Меняется только тело ответа. Остальное — от записи базы.

    Проверяются обе стороны разом, иначе тест проходил бы и при
    выключенном подключении: формулировка модели обязана появиться,
    а заголовок, светофор и блок USSD — остаться.
    """
    model(GOOD)
    chat = _Dialog(9101)

    chat.says("скока денег на счету")

    assert "смотрите через *100#" in chat.last, "формулировка модели не видна"
    assert "Проверка бесплатная." not in chat.last, "тело базы не заменено"
    assert "Как проверить баланс?" in chat.last, "заголовок темы потерян"
    assert "🟢" in chat.last, "светофор потерян"
    assert "*100#" in chat.last, "код потерян"


def test_foreign_code_leaves_the_base_answer(model):
    """Сторож фактов поймал чужой код — сообщение не переписывается вовсе."""
    model(FOREIGN_CODE)
    chat = _Dialog(9102)

    chat.says("скока денег на счету")

    assert chat.sent[0].edits == [], "ответ со чужим кодом попал абоненту"
    assert "*111#" not in chat.last
    assert "*100#" in chat.last


def test_invented_number_leaves_the_base_answer(model):
    """Цифры, которой нет в записи базы, абонент не увидит."""
    model(INVENTED_NUMBER)
    chat = _Dialog(9103)

    chat.says("скока денег на счету")

    assert chat.sent[0].edits == []
    assert "3 раза" not in chat.last


def test_model_is_silent_when_disabled(monkeypatch):
    """Выключенная переформулировка не обращается к сети ни разу."""
    from faq_bot import rephrase

    def forbidden(*args: object, **kwargs: object) -> dict:
        raise AssertionError("выключенный модуль полез в сеть")

    rephrase.reset()
    monkeypatch.setattr(rephrase, "ENABLED", False)
    monkeypatch.setattr(rephrase, "transport", forbidden)
    chat = _Dialog(9104)

    chat.says("скока денег на счету")

    assert chat.sent[0].edits == []
    assert "*100#" in chat.last


def test_refusal_never_reaches_the_model(model):
    """Отказ модели не показывают: переписывать нечего, платить не за что.

    Красный светофор означает, что темы нет. Отдать модели нечего:
    записи базы для пересказа не существует, а выдумывать ответ —
    ровно то, чего мы не делаем.
    """
    fake = model(GOOD)
    chat = _Dialog(9105)

    chat.says(OUT_OF_SCOPE)

    assert fake.calls == [], "за отказ заплатили модели"
    assert chat.sent[0].edits == []


def test_repeated_question_costs_nothing(model):
    """Второй такой же вопрос берётся из кеша, второго вызова нет."""
    fake = model(GOOD)
    _Dialog(9106).says("скока денег на счету")
    _Dialog(9107).says("скока денег на счету")

    assert len(fake.calls) == 1, "за повтор вопроса заплатили дважды"


# --------------------------------------------------------------------------
# Развод каналов: CRM и чат операторов не ведут одно обращение вдвоём
# --------------------------------------------------------------------------
#
# Дефект нашёлся при сборке CRM сокомандника с ботом. Просьба «оператор»
# ставила обращение И в очередь CRM, И полной карточкой в группу Telegram.
# Два человека, не видящие друг друга, могли написать абоненту разное.
# Второе: карточка выносит вопрос абонента дословно в групповой чат
# вместе с тем, что он написал сам — номером, адресом почты.


class _FakeHub:
    """CRM в объёме, который нужен боту: журнал и очередь."""

    def __init__(self, human: bool = False) -> None:
        self.requested: list[tuple[object, str]] = []
        self.messages: list[str] = []
        self._human = human

    def request_operator(self, chat_id: object, reason: str) -> None:
        self.requested.append((chat_id, reason))

    def client_message(self, chat_id: object, text: str, **kwargs: object) -> None:
        self.messages.append(text)

    def is_human(self, chat_id: object) -> bool:
        return self._human

    def bot_reply(self, chat_id: object, text: str, **kwargs: object) -> None:
        pass


@pytest.fixture
def crm(monkeypatch):
    """Подключить CRM на время теста и снять после."""
    hub = _FakeHub()
    monkeypatch.setattr(bot, "CRM", hub)
    return hub


SECRET = "баланс не тот, мой номер 0700112233"


def test_crm_takes_the_queue_and_the_group_gets_no_question(monkeypatch, crm):
    """С подключённой CRM в группу уходит уведомление без текста вопроса."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()

    _Dialog(9200, context=context).says(SECRET).says("оператор")

    assert crm.requested, "обращение не попало в очередь CRM"
    posted = context.bot.to(str(OPERATOR_CHAT))
    assert len(posted) == 1
    assert "0700112233" not in posted[0].text, "номер абонента ушёл в группу"
    assert SECRET not in posted[0].text
    assert "CRM" in posted[0].text


def test_a_reply_to_the_notice_does_not_reach_the_subscriber(monkeypatch, crm):
    """Отвечать можно только из CRM: реплай на уведомление не доставляется.

    Связь «сообщение → обращение» при ведущей CRM намеренно не создаётся,
    поэтому реплай в группе остаётся репликой в группе.
    """
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()
    _Dialog(9201, context=context).says("оператор")
    notice = context.bot.to(str(OPERATOR_CHAT))[0]
    before = len(context.bot.posted)

    _operator_says("наберите *100#", reply_to=notice.message_id,
                   context=context)

    assert len(context.bot.posted) == before, "ответ ушёл в обход CRM"


def test_without_crm_the_group_gets_the_full_card(monkeypatch):
    """Без CRM поведение прежнее: полная карточка и ответ реплаем."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    context = _FakeContext()

    _Dialog(9202, context=context).says(SECRET).says("оператор")

    posted = context.bot.to(str(OPERATOR_CHAT))
    assert "0700112233" in posted[0].text, "оператору не видно, с чем пришли"


def test_the_channel_can_be_forced_to_telegram(monkeypatch, crm):
    """`OPERATOR_CHANNEL=telegram` возвращает прежнее поведение при CRM."""
    pytest.importorskip("telegram")
    _operator_chat(monkeypatch)
    monkeypatch.setenv(bot.OPERATOR_CHANNEL_ENV, "telegram")
    context = _FakeContext()

    _Dialog(9203, context=context).says(SECRET).says("оператор")

    posted = context.bot.to(str(OPERATOR_CHAT))
    assert "0700112233" in posted[0].text


def test_an_unknown_channel_value_is_auto(monkeypatch):
    """Опечатка в настройке не меняет поведение молча."""
    monkeypatch.setenv(bot.OPERATOR_CHANNEL_ENV, "телеграмм")

    assert bot.operator_channel() == "auto"


def test_crm_alone_is_enough_to_take_the_request(monkeypatch, crm):
    """Без OPERATOR_CHAT_ID обращение принимает одна CRM, и этого хватает.

    Номер обращения — понятие карточки в чате операторов: без чата он не
    тратится (`handoff.DESK.remember` возвращает None). У CRM своя
    очередь и своя нумерация, поэтому абонент получает подтверждение без
    номера, а не тишину. Проверяется именно это: запрос дошёл до CRM, в
    Telegram не ушло ничего, абонент не остался без ответа.
    """
    pytest.importorskip("telegram")
    monkeypatch.delenv(handoff.OPERATOR_CHAT_ENV, raising=False)
    context = _FakeContext()

    chat = _Dialog(9204, context=context).says("оператор")

    assert crm.requested, "CRM не получила запрос"
    assert context.bot.posted == [], "ушло в Telegram без настроенного чата"
    assert "оператор" in chat.last.lower(), "абонент не получил подтверждения"


# --------------------------------------------------------------------------
# Оценка работы после молчания
# --------------------------------------------------------------------------
#
# Спрашиваем не сразу после ответа, а когда разговор кончился: только
# тогда оценка относится к разговору целиком, а не к одной реплике.
# И не чаще раза в несколько часов — бот, просящий оценку после каждого
# вопроса, это спам, а не обратная связь.

SILENCE = 300.0 + 1


def test_silence_is_what_triggers_the_question():
    """Пока абонент пишет, об оценке не спрашивают."""
    bot.note_activity(9300)

    assert bot.chats_to_ask() == []
    assert bot.chats_to_ask(now=time.time() + SILENCE) == ["9300"]


def test_the_same_chat_is_not_asked_twice_in_a_row():
    """Одно молчание — одна просьба, и повтор отсечён по времени."""
    bot.note_activity(9301)
    moment = time.time() + SILENCE
    assert bot.chats_to_ask(now=moment) == ["9301"]

    bot.note_activity(9301)

    assert bot.chats_to_ask(now=moment + SILENCE) == [], "спросили дважды подряд"


def test_the_rating_card_offers_digits_not_words():
    """Кнопки — цифры: цифра не имеет языка, как и в USSD-меню."""
    screen = presentation.rating_screen("ky")

    assert screen.actions() == ("rate:1", "rate:2", "rate:3", "rate:4", "rate:5")


def test_a_rating_is_counted_and_the_card_is_redrawn():
    from faq_bot.answer import stats_snapshot

    screen = bot.screen_for_action("rate:5", chat_id=9302)

    assert "5 / 5" in screen.text
    assert screen.actions() == (), "кнопки остались, можно оценить дважды"
    assert stats_snapshot().ratings_total == 1
    assert stats_snapshot().rating_average == 5.0


def test_a_low_rating_offers_a_human():
    """«Спасибо» в ответ на «не помог» — это тупик."""
    screen = bot.screen_for_action("rate:1", chat_id=9303)

    assert "оператор" in screen.text.lower()


def test_a_rating_outside_the_scale_changes_nothing():
    from faq_bot.answer import stats_snapshot

    for action in ("rate:0", "rate:9", "rate:", "rate:пять"):
        bot.screen_for_action(action, chat_id=9304)

    assert stats_snapshot().ratings_total == 0


def test_the_question_is_not_asked_while_a_human_leads(monkeypatch):
    """Посреди работы оператора «помог ли я» мешает обоим."""
    pytest.importorskip("telegram")

    class _Busy(_FakeHub):
        def is_human(self, chat_id: object) -> bool:
            return True

    monkeypatch.setattr(bot, "CRM", _Busy())
    bot.note_activity(9305)
    context = _FakeContext()

    asked = asyncio.run(bot.ask_ratings(context, now=time.time() + SILENCE))

    assert asked == 0
    assert context.bot.posted == []


def test_the_card_reaches_a_silent_chat(monkeypatch):
    pytest.importorskip("telegram")
    monkeypatch.setattr(bot, "CRM", None)
    bot.note_activity(9306)
    context = _FakeContext()

    asked = asyncio.run(bot.ask_ratings(context, now=time.time() + SILENCE))

    assert asked == 1
    assert "rate:" in str(context.bot.posted[0].markup) or context.bot.posted
