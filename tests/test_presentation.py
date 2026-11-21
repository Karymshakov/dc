"""Тесты интерфейса: USSD-меню, карточка ответа, светофор, /stats.

Оформление проверяется без Telegram: `presentation` возвращает текст и
описание кнопок, а `bot.screen_for_*` решает, какой экран показать. Это
и есть пункт 3 обязательной части — «ниже порога показать три похожих
вопроса кнопками».

Отдельная группа тестов охраняет правило «один язык за раз». ТЗ требует
отвечать на языке вопроса, а не на двух сразу: двуязычный экран вдвое
длиннее и делает кнопку переключения языка бессмысленной. Исключение
ровно одно — `/start`, где язык абонента ещё неизвестен.
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


def test_menu_lines_are_numbered_and_single_language():
    """Строка меню: цифра и название раздела на языке абонента.

    Цифра языка не имеет и работает одинаково для обеих аудиторий —
    поэтому дублировать само название вторым языком не нужно.
    """
    kyrgyz = presentation.plain_text(presentation.menu_screen("ky"))
    russian = presentation.plain_text(presentation.menu_screen("ru"))

    for section in presentation.MENU_SECTIONS:
        assert section.title_ky in kyrgyz
        assert section.title_ru not in kyrgyz
        assert section.title_ru in russian
        assert section.title_ky not in russian
        assert str(section.number) in kyrgyz


def test_start_is_the_only_bilingual_screen():
    """`/start` двуязычен: абонент ещё ничего не написал.

    Определять язык не по чему, а встречать кыргызоязычного человека
    русским приветствием только потому, что `ru` стоит по умолчанию, —
    хуже, чем одна длинная строка. После первой реплики язык известен,
    и дальше интерфейс одноязычный.
    """
    start = presentation.plain_text(presentation.start_screen("ru"))

    assert "Саламатсызбы! · Здравствуйте!" in start
    for section in presentation.MENU_SECTIONS:
        assert f"{section.title_ky} · {section.title_ru}" in start


def test_start_screen_is_menu_not_language_choice():
    """Экрана «выберите язык» нет: язык определяется сам."""
    screen = presentation.start_screen("ru")
    language_buttons = [button for button in screen.buttons
                        if button.action.startswith("lang:")]

    assert len(language_buttons) == 1
    assert any(button.action.startswith("section:") for button in screen.buttons)


def test_section_screen_prints_each_topic_once():
    """Тема в списке раздела печатается один раз, а не с переводом под ней."""
    section = presentation.section_by_number(2)
    text = presentation.plain_text(
        presentation.section_screen(section, all_entries(), "ru"))

    for faq_id in section.faq_ids:
        entry = entry_by_id(faq_id)
        assert text.count(entry.question_ru) == 1
        assert entry.question_ky not in text


#: Служебные слова, по которым видно, что в экран затесался второй язык.
KY_SERVICE_WORDS = ("жазыңыз", "басыңыз", "тандаңыз", "суроо", "кодду")
RU_SERVICE_WORDS = ("нажмите", "напишите", "выберите", "вопрос",
                    "скопировать")


def _screens(language: str) -> list[Screen]:
    """Экраны, которые абонент видит чаще всего."""
    return [
        presentation.menu_screen(language),
        presentation.section_screen(presentation.section_by_number(2),
                                    all_entries(), language),
        presentation.answer_card(make_reply(1, language=language)),
        presentation.answer_card(rejected_reply(language)),
        presentation.acknowledged_screen(language),
        presentation.acknowledged_card(make_reply(1, language=language)),
        presentation.another_question_screen(language),
        presentation.another_question_card(make_reply(1, language=language)),
        presentation.alternatives_screen(
            [entry_by_id(2), entry_by_id(10)], language, entry_by_id(3)),
        presentation.listening_screen(language),
        presentation.help_screen(language),
        presentation.operator_screen(entry_by_id(14), language,
                                     "direct_request"),
        presentation.with_ticket_note(
            presentation.operator_screen(entry_by_id(14), language,
                                         "direct_request"), "0001", language),
        presentation.operator_answer_screen("—", language),
    ]


@pytest.mark.parametrize("language, foreign", [
    ("ru", KY_SERVICE_WORDS),
    ("ky", RU_SERVICE_WORDS),
])
def test_screens_do_not_mix_two_languages(language, foreign):
    """В экране нет служебных слов второго языка.

    Это и был дефект: подсказки, подписи кнопок и строка состояния
    печатались на двух языках, экран раздувался вдвое при той же пользе,
    а кнопка переключения языка ничего видимого не меняла.
    """
    for screen in _screens(language):
        text = presentation.plain_text(screen).lower()
        for word in foreign:
            assert word not in text, (word, text)


def test_language_status_line_is_gone():
    """Длинной строки «язык ответов: Русский (определяется сам)» больше нет.

    Она требовалась, пока смена языка ничего не меняла на экране. Теперь
    меню само написано на выбранном языке.
    """
    text = presentation.plain_text(presentation.menu_screen("ru"))

    assert "язык ответов" not in text
    assert "Жооп тили" not in text
    assert not hasattr(presentation, "language_status")


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
    """«0» — оператор, как и положено в USSD-меню.

    Экран подтверждает передачу и оставляет из темы 14 только факт —
    часы работы. Инструкцию «напишите „оператор“» человеку, который
    только что нажал «0», не цитируем.
    """
    text = presentation.plain_text(bot.screen_for_text("0"))

    assert presentation.operator_facts(entry_by_id(14), "ru") in text
    assert "Напишите «оператор»" not in text


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


def test_card_is_written_in_one_language():
    """ТЗ: «отвечает на том же языке». Второго перевода в карточке нет.

    Блок «Русский: …» под кыргызским ответом удваивал карточку и делал
    переключатель языка бессмысленным: что ни нажми, на экране всё равно
    оба языка.
    """
    entry = entry_by_id(4)
    screen = presentation.answer_card(make_reply(4, language="ky"))

    assert entry.answer_ky in screen.text
    assert entry.answer_ru not in screen.text
    assert f"{presentation.LANGUAGE_NAMES['ru']}:" not in screen.text


def test_card_language_button_redraws_the_same_card():
    """Кнопка языка несёт адрес своего экрана, а не просто код языка.

    Благодаря этому `bot.screen_for_action()` собирает ту же карточку на
    другом языке, а `bot._on_button()` переписывает ею то же сообщение.
    """
    screen = presentation.answer_card(make_reply(4, language="ky"))
    actions = [button.action for button in screen.buttons
               if button.action.startswith("lang:")]

    assert actions == ["lang:ru:faq:4"]


def test_ack_keeps_the_answer_and_drops_only_the_keyboard():
    """«Понятно» сохраняет ответ: стирать его нажатием кнопки нельзя.

    Дефект живого теста: нажатие ✅ переписывало карточку одной строкой
    «✅ Болду», и ответ про интернет исчезал из переписки — в истории
    оставалось, будто бот ответил «Болду» на вопрос про интернет.
    """
    entry = entry_by_id(3)
    screen = presentation.acknowledged_card(make_reply(3))
    text = presentation.plain_text(screen)

    assert entry.answer_ru in text              # ответ остался дословно
    assert entry.question_ru in text            # и тема тоже
    assert text.startswith("✅")                 # вопрос закрыт
    assert screen.rows == ()                    # клавиатуры больше нет
    assert "просто напишите" in text


def test_another_question_keeps_the_answer_too():
    """«Другой вопрос» тоже не стирает ответ — по той же причине."""
    screen = presentation.another_question_card(make_reply(3))
    text = presentation.plain_text(screen)

    assert entry_by_id(3).answer_ru in text
    assert "останется здесь" in text
    assert screen.actions() == ("menu", "operator")


def test_ack_without_a_topic_number_still_answers():
    """Кнопка из старого сообщения: карточку собрать не из чего."""
    screen = presentation.acknowledged_screen("ru")

    assert "\n" not in screen.text
    assert screen.rows == ()


@pytest.mark.parametrize("screen, limit", [
    (presentation.answer_card(make_reply(1)), 7),
    (presentation.answer_card(rejected_reply()), 7),
    (presentation.menu_screen("ru"), 10),
    (presentation.acknowledged_screen("ru"), 1),
    (presentation.another_question_screen("ru"), 2),
    (presentation.smalltalk_screen("greeting", "ru"), 3),
    (presentation.acknowledged_card(make_reply(1)), 9),
    (presentation.alternatives_screen([entry_by_id(2), entry_by_id(10)],
                                      "ru", entry_by_id(3)), 7),
])
def test_screens_stay_short(screen, limit):
    """Карточка и служебные экраны держатся в границах по числу строк.

    Числа взяты из живого теста: двадцать сообщений за минуту означали,
    что ответ найти невозможно. Короткий экран — часть ответа, а не
    украшение.
    """
    lines = presentation.plain_text(screen).split("\n")

    assert len(lines) <= limit, "\n".join(lines)


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
    # Номер темы едет в самой кнопке: «Понятно» пересобирает эту же
    # карточку, а не затирает её одной строкой.
    assert "ack:1" in screen.actions()
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

    # Факты из базы без изменений; длинное тире заменено обычным знаком.
    assert presentation.plain_dashes(entry_by_id(faq_id).answer_ru) in text


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
    """«Другой вопрос» сбрасывает тему диалога и приглашает написать.

    Полного меню здесь нет намеренно: абонент только что сказал, что
    хочет спросить своими словами. Меню — одной кнопкой.
    """
    from faq_bot.answer import MEMORY

    MEMORY.remember(7, 4, 0.9)
    screen = bot.screen_for_action("another:4", 7)
    text = presentation.plain_text(screen)

    assert MEMORY.recent_topic(7) is None
    assert entry_by_id(4).answer_ru in text      # ответ при этом остался
    assert "menu" in screen.actions()
    assert not [action for action in screen.actions()
                if action.startswith("section:")]


@pytest.mark.parametrize("action", ["ack", "another", "ack:1", "another:1",
                                    "faq:1", "section:2"])
def test_menu_is_not_repeated_after_every_button(action):
    """Полное меню приходит по /start, по кнопке «Меню» и при отказе.

    Раньше оно печаталось ещё и после приветствия, после «Понятно» и
    после каждого ответа — абонент видел шесть пунктов меню чаще, чем
    сам ответ.
    """
    text = presentation.plain_text(bot.screen_for_action(action, 7))
    listed = [section for section in presentation.MENU_SECTIONS
              if section.name("ru") in text]

    assert len(listed) < len(presentation.MENU_SECTIONS)


def test_menu_button_still_shows_the_whole_menu():
    """А по кнопке «Меню» — показывает: это один из трёх разрешённых мест."""
    text = presentation.plain_text(bot.screen_for_action("menu", 7))

    for section in presentation.MENU_SECTIONS:
        assert section.name("ru") in text


def test_unknown_action_falls_back_to_menu():
    """Непонятная кнопка не ломает бота."""
    screen = bot.screen_for_action("что-то неизвестное", 7)

    assert any(action.startswith("section:") for action in screen.actions())


def test_operator_word_reaches_operator():
    """Бонус ТЗ: слово «оператор» ведёт к живому оператору."""
    text = presentation.plain_text(bot.screen_for_text("оператор", 7))

    assert "Попросили оператора" in text
    assert presentation.operator_facts(entry_by_id(14), "ru") in text


# --------------------------------------------------------------------------
# Абсурд на слове «оператор»: инструкция вместо подтверждения
# --------------------------------------------------------------------------

def test_handoff_does_not_quote_the_instruction_it_was_just_followed():
    """Живой тест: «оператор» → «Напишите „оператор“ в боте».

    Текст темы 14 написан как ответ на вопрос «как связаться с
    поддержкой». Человеку, который эту инструкцию только что выполнил,
    она бессмысленна, поэтому при известной причине передачи экран её не
    цитирует.
    """
    screen = presentation.operator_screen(entry_by_id(14), "ru",
                                          "direct_request")
    text = presentation.plain_text(screen)

    assert "Напишите «оператор» в боте" not in text
    assert entry_by_id(14).answer_ru not in text
    assert "Обращение принято" in text


def test_handoff_keeps_the_facts_from_the_base():
    """Часы работы остаются: факты берём из faq.json, а не сочиняем."""
    text = presentation.plain_text(
        presentation.operator_screen(entry_by_id(14), "ru", "complaint"))

    assert "09:00–18:00" in text
    assert "09:00–18:00" in entry_by_id(14).answer_ru


@pytest.mark.parametrize("language, marker", [
    ("ru", "Учебная поддержка работает ежедневно 09:00–18:00."),
    ("ky", "Окуу колдоо кызматы күн сайын 09:00–18:00 иштейт."),
])
def test_operator_facts_are_parsed_not_rewritten(language, marker):
    """Факт разбирается из ответа темы 14 дословно, на двух языках."""
    facts = presentation.operator_facts(entry_by_id(14), language)

    assert facts == marker
    assert facts in entry_by_id(14).answer(language)
    assert "жазыңыз" not in facts and "Напишите" not in facts


def test_operator_facts_survive_a_base_without_hours():
    """В базе нет времени — остаётся всё, кроме самой инструкции.

    Правка faq.json не имеет права превращать экран передачи в молчание.
    """
    from faq_bot.contracts import FaqEntry

    entry = FaqEntry(14, "вопрос", "суроо",
                     "Напишите «оператор» в боте. Поддержка работает.",
                     "Ботко «оператор» деп жазыңыз. Колдоо иштейт.")

    assert presentation.operator_facts(entry, "ru") == "Поддержка работает."
    assert presentation.operator_facts(entry, "ky") == "Колдоо иштейт."
    assert presentation.operator_facts(None, "ru") == ""


def test_a_question_about_support_still_gets_the_instruction():
    """А вопрос «как связаться с поддержкой» инструкцию получает.

    Причина передачи не указана: тема 14 пришла обычным путём, и
    инструкция из базы здесь уместна.
    """
    text = presentation.plain_text(
        presentation.operator_screen(entry_by_id(14), "ru"))

    assert presentation.plain_dashes(entry_by_id(14).answer_ru) in text


def test_topic_14_by_button_is_an_ordinary_card():
    """Кнопка «как связаться с оператором» — обычная карточка темы."""
    text = presentation.plain_text(bot.screen_for_action("faq:14", 7))

    assert presentation.plain_dashes(entry_by_id(14).answer_ru) in text


def test_ticket_note_is_added_only_after_real_delivery():
    """Номер обращения и обещание ответа дописываются к готовому экрану."""
    screen = presentation.operator_screen(entry_by_id(14), "ru",
                                          "direct_request")
    noted = presentation.with_ticket_note(screen, "0007", "ru")

    assert "0007" not in presentation.plain_text(screen)
    assert "0007" in presentation.plain_text(noted)
    assert "ответ придёт в этот же чат" in presentation.plain_text(noted)
    assert noted.rows == screen.rows


@pytest.mark.parametrize("language, title", [
    ("ru", "🧑‍💼 Ответ оператора"),
    ("ky", "🧑‍💼 Оператордун жообу"),
])
def test_operator_answer_is_marked_as_a_human_answer(language, title):
    """Слова человека помечены: это другое обещание, чем ответ из базы."""
    text = presentation.plain_text(
        presentation.operator_answer_screen("Проверил, всё включено.",
                                            language))

    assert text.startswith(title)
    assert "Проверил, всё включено." in text


def test_operator_answer_is_trimmed_and_escaped():
    """Длинный ответ обрезается, разметка абонента не ломает сообщение."""
    screen = presentation.operator_answer_screen("<b>дома</b> & на работе")
    long_answer = presentation.operator_answer_screen(
        "я" * (presentation.OPERATOR_REPLY_LENGTH + 500))

    assert "&lt;b&gt;" in screen.text
    assert "&amp;" in screen.text
    assert len(long_answer.text) < presentation.OPERATOR_REPLY_LENGTH + 200


def test_all_button_actions_fit_telegram_limit():
    """callback_data у Telegram ограничен 64 байтами."""
    screens = [presentation.menu_screen("ru"),
               presentation.answer_card(make_reply(1, confidence=0.45)),
               presentation.answer_card(rejected_reply()),
               presentation.alternatives_screen(
                   [entry_by_id(2), entry_by_id(10)], "ru", entry_by_id(3)),
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


@pytest.mark.parametrize("raw, expected", [
    ("1 ГБ — 30 сом, 5 ГБ — 100 сом", "1 ГБ: 30 сом, 5 ГБ: 100 сом"),
    ("ежедневно 09:00–18:00", "ежедневно 09:00–18:00"),
    ("1 — совсем не помог, 5 — всё решил", "1: совсем не помог, 5: всё решил"),
    ("Скажите короче — или напишите текстом", "Скажите короче или напишите текстом"),
    ("Оператор видит обращение — ответ придёт сюда",
     "Оператор видит обращение, ответ придёт сюда"),
    ("без тире", "без тире"),
])
def test_plain_dashes_changes_the_sign_not_the_facts(raw, expected):
    """Длинное тире уходит, числа и слова остаются на месте."""
    assert presentation.plain_dashes(raw) == expected


def test_no_screen_text_of_ours_has_a_long_dash():
    """Свои тексты бот пишет без длинного тире: меню, справка, все темы."""
    screens = [presentation.start_screen("ru"), presentation.start_screen("ky"),
               presentation.help_screen("ru"), presentation.help_screen("ky")]
    screens += [bot.screen_for_action(f"faq:{entry.id}") for entry in all_entries()]
    for screen in screens:
        assert "—" not in screen.text, screen.text
