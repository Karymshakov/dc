"""Тесты интерфейса: USSD-меню, двуязычная карточка, светофор, /stats.

Оформление проверяется без Telegram: `presentation` возвращает текст и
описание кнопок, а `bot.screen_for_*` решает, какой экран показать. Это
и есть пункт 3 обязательной части — «ниже порога показать три похожих
вопроса кнопками».
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from faq_bot import bot, config, presentation
from faq_bot.answer import Reply, all_entries, entry_by_id
from faq_bot.presentation import Button, Screen
from faq_bot.stats import StatsCollector


@pytest.fixture(autouse=True)
def clean_bot_state():
    """Язык интерфейса не переносится между тестами."""
    bot._chat_language.clear()
    bot._chat_language_pinned.clear()
    yield
    bot._chat_language.clear()
    bot._chat_language_pinned.clear()


def make_reply(faq_id: int = 1, confidence: float = 0.9, language: str = "ru",
               suggestions: tuple[int, ...] = (1, 2, 11),
               inherited: bool = False) -> Reply:
    """Собрать результат так, как его собирает `respond()`."""
    entry = entry_by_id(faq_id)
    ordered = tuple(dict.fromkeys(([faq_id] if entry else []) + list(suggestions)))
    return Reply(
        text="тест",
        lang=language,
        faq_id=entry.id if entry else None,
        confidence=confidence,
        candidates=(entry.id,) if entry else (),
        suggestions=ordered,
        entry=entry,
        suggestion_entries=tuple(
            found for found in (entry_by_id(item) for item in ordered)
            if found is not None
        ),
        inherited_from_context=inherited,
    )


def rejected_reply(language: str = "ru",
                   suggestions: tuple[int, ...] = (2, 3, 6)) -> Reply:
    """Результат ниже порога: темы нет, похожие вопросы есть."""
    return Reply(
        text="тест",
        lang=language,
        faq_id=None,
        confidence=0.1,
        candidates=(),
        suggestions=suggestions,
        entry=None,
        suggestion_entries=tuple(entry_by_id(item) for item in suggestions),
    )


# --------------------------------------------------------------------------
# USSD-меню
# --------------------------------------------------------------------------

def test_menu_covers_every_faq_id_exactly_once():
    """Все 15 тем разложены по разделам без потерь и повторов."""
    ids = [faq_id for section in presentation.MENU_SECTIONS
           for faq_id in section.faq_ids]

    assert sorted(ids) == sorted(entry.id for entry in all_entries())
    assert len(ids) == len(set(ids)) == config.FAQ_SIZE


def test_menu_has_five_sections_plus_operator():
    """Пять понятных разделов и «0 Оператор»."""
    numbers = [section.number for section in presentation.MENU_SECTIONS]

    assert numbers[-1] == 0
    assert 5 <= len(numbers) <= 7
    assert len(set(numbers)) == len(numbers)


def test_menu_lines_are_bilingual_and_numbered():
    """Строка меню содержит цифру и оба языка сразу."""
    text = presentation.plain_text(presentation.menu_screen("ky"))

    for section in presentation.MENU_SECTIONS:
        assert section.title_ky in text
        assert section.title_ru in text
        assert str(section.number) in text
        assert f"{section.title_ky} · {section.title_ru}" in text


def test_start_screen_is_menu_not_language_choice():
    """Экрана «выберите язык» нет: язык определяется сам."""
    screen = presentation.start_screen("ru")
    language_buttons = [button for button in screen.buttons
                        if button.action.startswith("lang:")]

    assert len(language_buttons) == 1
    assert any(button.action.startswith("section:") for button in screen.buttons)


def test_section_screen_lists_questions_as_buttons():
    """Раздел меню открывает вопросы кнопками с их faq_id."""
    section = presentation.section_by_number(2)
    screen = presentation.section_screen(section, all_entries(), "ru")
    actions = [action for action in screen.actions()
               if action.startswith("faq:")]

    assert actions == [f"faq:{faq_id}" for faq_id in section.faq_ids]


def test_digit_input_opens_section():
    """Цифра работает как в USSD: открывает раздел."""
    screen = bot.screen_for_text("5")
    text = presentation.plain_text(screen)

    assert entry_by_id(4).question_ru in text
    assert "faq:4" in screen.actions()


def test_zero_calls_operator():
    """«0» — оператор, как и положено в USSD-меню."""
    text = presentation.plain_text(bot.screen_for_text("0"))

    assert entry_by_id(14).answer_ru in text


def test_free_input_always_works():
    """Свободный ввод не требует меню."""
    screen = bot.screen_for_text("Как проверить баланс?")

    assert entry_by_id(1).answer_ru in presentation.plain_text(screen)


# --------------------------------------------------------------------------
# Двуязычная карточка и USSD-код
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Наберите *100#. Проверка бесплатная.", ("*100#",)),
    ("*145*номер*сумма#, затем подтверждение.", ("*145*номер*сумма#",)),
    ("В *111# выберите 1 Интернет", ("*111#",)),
    ("Посмотрите номер в личном кабинете.", ()),
])
def test_extract_ussd_codes(text, expected):
    assert presentation.extract_ussd_codes(text) == expected


def test_card_puts_ussd_code_into_separate_block():
    """Код вынесен моноширинным блоком — копируется одним касанием."""
    screen = presentation.answer_card(make_reply(1))

    assert "<code>*100#</code>" in screen.text
    assert "\n<code>*100#</code>\n" in screen.text


def test_card_without_ussd_code_has_no_block():
    """Нет кода в ответе — нет и блока."""
    screen = presentation.answer_card(make_reply(12))

    assert "<code>" not in screen.text


def test_card_shows_both_languages():
    """Ответ на языке вопроса крупно, второй язык — мелко, курсивом."""
    entry = entry_by_id(4)
    screen = presentation.answer_card(make_reply(4, language="ky"))

    assert entry.answer_ky in screen.text
    assert f"<i>{presentation.LANGUAGE_NAMES['ru']}: {entry.answer_ru}</i>" \
        in screen.text


def test_card_quotes_answer_from_faq_json():
    """В карточке нет выдуманных фактов: только текст темы."""
    entry = entry_by_id(9)
    text = presentation.plain_text(presentation.answer_card(make_reply(9)))

    assert entry.answer_ru in text


def test_card_marks_inherited_topic():
    """Унаследованная тема честно помечена."""
    text = presentation.plain_text(
        presentation.answer_card(make_reply(4, inherited=True)))

    assert "предыдущей теме" in text


def test_user_text_is_escaped_in_stats():
    """Разметка из чужого сообщения не ломает экран и не исполняется."""
    collector = StatsCollector()
    collector.record("<b>жирный</b> вопрос", "ru", faq_id=None)
    screen = presentation.stats_screen(collector.snapshot(), all_entries())

    assert "<b>жирный</b>" not in screen.text
    assert "&lt;b&gt;жирный&lt;/b&gt;" in screen.text


# --------------------------------------------------------------------------
# Светофор уверенности: три разных режима
# --------------------------------------------------------------------------

def test_green_light_answers_immediately():
    """🟢 Выше верхнего порога — ответ сразу, без уточнений."""
    screen = presentation.answer_card(
        make_reply(1, confidence=config.CONFIDENCE_CONFIDENT + 0.1))

    assert screen.text.startswith("🟢")
    assert entry_by_id(1).answer_ru in screen.text
    assert "ack" in screen.actions()
    assert not [action for action in screen.actions()
                if action.startswith("faq:")]


def test_yellow_light_answers_and_suggests():
    """🟡 Между порогами — ответ и «может быть, вы про это?» с кнопками."""
    middle = (config.CONFIDENCE_THRESHOLD + config.CONFIDENCE_CONFIDENT) / 2
    screen = presentation.answer_card(make_reply(1, confidence=middle))
    suggestions = [action for action in screen.actions()
                   if action.startswith("faq:")]

    assert screen.text.startswith("🟡")
    assert entry_by_id(1).answer_ru in screen.text
    assert "может быть, вы про это?" in screen.text
    assert suggestions and "faq:1" not in suggestions


def test_red_light_shows_three_buttons_without_answer():
    """🔴 Ниже порога — ответа нет, только три похожих вопроса кнопками."""
    screen = presentation.answer_card(rejected_reply())
    suggestions = [action for action in screen.actions()
                   if action.startswith("faq:")]

    assert screen.text.startswith("🔴")
    assert len(suggestions) == config.TOP_K
    for faq_id in (2, 3, 6):
        assert entry_by_id(faq_id).answer_ru not in screen.text
    assert "menu" in screen.actions()


def test_three_levels_give_three_different_screens():
    """Три уровня уверенности — три разных экрана."""
    low = presentation.answer_card(rejected_reply())
    middle = presentation.answer_card(make_reply(1, confidence=0.45))
    high = presentation.answer_card(make_reply(1, confidence=0.95))

    marks = {screen.text[0] for screen in (low, middle, high)}
    assert marks == {"🔴", "🟡", "🟢"}
    assert len({screen.text for screen in (low, middle, high)}) == 3


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------

@pytest.mark.parametrize("faq_id", [entry.id for entry in all_entries()])
def test_button_press_answers_that_faq_id(faq_id):
    """Нажатие кнопки обязано выдать ответ именно по этой теме."""
    screen = bot.screen_for_action(f"faq:{faq_id}")
    text = presentation.plain_text(screen)

    assert entry_by_id(faq_id).answer_ru in text


def test_button_press_keeps_chat_language():
    """Кнопка отвечает на языке интерфейса этого чата."""
    bot.set_chat_language(77, "ky")
    text = presentation.plain_text(bot.screen_for_action("faq:1", 77))

    assert entry_by_id(1).answer_ky in text


def test_language_button_switches_interface():
    """Переключатель языка — одна кнопка, не экран на старте."""
    screen = bot.screen_for_action("lang:ky", 7)

    assert bot.chat_language(7) == "ky"
    assert any(section.title_ky in presentation.plain_text(screen)
               for section in presentation.MENU_SECTIONS)


def test_another_question_button_resets_topic():
    """«Другой вопрос» сбрасывает тему диалога и показывает меню."""
    from faq_bot.answer import MEMORY

    MEMORY.remember(7, 4, 0.9)
    screen = bot.screen_for_action("another", 7)

    assert MEMORY.recent_topic(7) is None
    assert "Новый вопрос" in presentation.plain_text(screen)
    assert any(action.startswith("section:") for action in screen.actions())


def test_unknown_action_falls_back_to_menu():
    """Непонятная кнопка не ломает бота."""
    screen = bot.screen_for_action("что-то неизвестное", 7)

    assert any(action.startswith("section:") for action in screen.actions())


def test_operator_word_reaches_operator():
    """Бонус ТЗ: слово «оператор» ведёт к живому оператору."""
    text = presentation.plain_text(bot.screen_for_text("оператор", 7))

    assert entry_by_id(14).answer_ru in text


def test_all_button_actions_fit_telegram_limit():
    """callback_data у Telegram ограничен 64 байтами."""
    screens = [presentation.menu_screen("ru"),
               presentation.answer_card(make_reply(1, confidence=0.45)),
               presentation.answer_card(rejected_reply()),
               presentation.help_screen("ky")]

    for screen in screens:
        for button in screen.buttons:
            assert len(button.action.encode("utf-8")) <= 64
            assert button.label.strip()


# --------------------------------------------------------------------------
# /stats
# --------------------------------------------------------------------------

def test_stats_screen_on_empty_history():
    """/stats на пустой истории не падает."""
    screen = bot.stats_screen()

    assert "пока вопросов не было" in presentation.plain_text(screen)


def test_stats_screen_shows_aggregate():
    """Сколько не нашли, какие темы и примеры формулировок."""
    collector = StatsCollector()
    collector.record("Как проверить баланс?", "ru", faq_id=1)
    collector.record("Прогноз погоды", "ru", faq_id=None, nearest_faq_id=3)
    collector.record("Футбол эсеби", "ky", faq_id=None, nearest_faq_id=3)
    text = presentation.plain_text(
        presentation.stats_screen(collector.snapshot(), all_entries()))

    assert "всего вопросов" in text
    assert "67%" in text
    assert entry_by_id(3).question_ru[:20] in text
    assert "«Прогноз погоды»" in text


def test_stats_screen_hides_personal_data():
    """Агрегат вместо ленты чужих сообщений."""
    collector = StatsCollector()
    collector.record("Переведите 500 на 996770123456 срочно", "ru",
                     faq_id=None, chat_id=12345, nearest_faq_id=9)
    collector.record("напишите мне на user@mail.example", "ru",
                     faq_id=None, chat_id=777)
    text = presentation.plain_text(
        presentation.stats_screen(collector.snapshot(), all_entries()))

    assert "996770123456" not in text
    assert "12345" not in text
    assert "user@mail.example" not in text
    assert "без персональных данных" in text


# --------------------------------------------------------------------------
# Типы интерфейса
# --------------------------------------------------------------------------

def test_screen_and_button_are_immutable():
    """Экран — данные, а не состояние."""
    screen = Screen("текст", ((Button("метка", "menu"),),))

    with pytest.raises(FrozenInstanceError):
        screen.text = "другое"       # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        screen.buttons[0].label = "иное"   # type: ignore[misc]


def test_presentation_does_not_import_telegram():
    """Оформление тестируется без Telegram."""
    source = (presentation.__file__)
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()

    assert "import telegram" not in text
    assert "from telegram" not in text
