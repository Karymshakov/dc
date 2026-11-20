"""Оформление ответов: USSD-меню, двуязычная карточка, светофор уверенности.

Слой намеренно отделён от Telegram: здесь только чистые функции, которые
возвращают текст и описание кнопок (`Screen`). Благодаря этому интерфейс
проверяется обычными тестами, а `bot.py` занимается лишь превращением
`Screen` в сообщение.

Три идеи, на которых держится интерфейс.

1. **USSD-меню как вход.** Вся предметная область — это `*100#` и прочие
   короткие коды, абоненты к ним привыкли. Меню пронумеровано и двуязычно
   в одну строку: цифра не имеет языка, и это самый доступный элемент
   интерфейса для двуязычной аудитории. Свободный ввод работает всегда,
   меню — запасной путь, а не обязательный шаг.
2. **Двуязычная карточка.** Ответ на языке вопроса — крупно; USSD-код
   вынесен отдельным моноширинным блоком, чтобы копировался одним
   касанием; тот же ответ вторым языком — мелко, курсивом.
3. **Светофор уверенности.** 🟢 уверенный ответ, 🟡 ответ плюс «может
   быть, вы про это?», 🔴 ответа нет, только три похожих вопроса
   кнопками. Порог перестаёт быть числом в конфиге и становится видимым
   поведением.

Экрана «выберите язык» нет: язык определяется сам, переключатель спрятан
в одну кнопку, и выбранное состояние видно в меню.

Тексты и списки слов не зашиты в код: они лежат в `faq_bot/bot_rules.json`
и читаются загрузчиком :func:`rules`. Правит их человек без Python,
а если файла нет или он битый — работают встроенные значения
:data:`DEFAULT_RULES` и в лог уходит предупреждение.
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .answer import Reply
from .context import mentions_topic
from .contracts import FaqEntry
from .normalize import fold
from .stats import StatsSnapshot

logger = logging.getLogger(__name__)

# Коды вида *100#, *102#, *145*номер*сумма#.
USSD_RE = re.compile(r"\*\d[\w*#]*#", re.UNICODE)

LANGUAGE_NAMES: Mapping[str, str] = {"ru": "Русский", "ky": "Кыргызча"}
BUTTON_LABEL_LENGTH = 38


@dataclass(frozen=True)
class Button:
    """Кнопка экрана.

    `action` — строка-команда для `bot.py`: `faq:<id>`, `menu`,
    `section:<n>`, `operator`, `ack`, `another`, `lang:<ru|ky>`, `stats`.
    Telegram здесь не упоминается намеренно.
    """

    label: str
    action: str


@dataclass(frozen=True)
class Screen:
    """Готовое сообщение: текст и ряды кнопок."""

    text: str
    rows: tuple[tuple[Button, ...], ...] = ()
    parse_mode: str = "HTML"

    @property
    def buttons(self) -> tuple[Button, ...]:
        """Все кнопки одним списком."""
        return tuple(button for row in self.rows for button in row)

    def actions(self) -> tuple[str, ...]:
        """Команды всех кнопок — удобно тестам."""
        return tuple(button.action for button in self.buttons)


@dataclass(frozen=True)
class MenuSection:
    """Раздел USSD-меню: цифра, двуязычное название и темы FAQ."""

    number: int
    icon: str
    short: str
    title_ky: str
    title_ru: str
    faq_ids: tuple[int, ...]

    @property
    def title(self) -> str:
        """Двуязычное название в одну строку."""
        return f"{self.title_ky} · {self.title_ru}"


# Пятнадцать тем сгруппированы в пять разделов плюс «0 Оператор».
# Короткие подписи кнопок совпадают в обоих языках — это тоже доступность.
MENU_SECTIONS: tuple[MenuSection, ...] = (
    MenuSection(1, "💰", "Баланс", "Баланс жана төлөм",
                "Баланс и оплата", (1, 11, 9)),
    MenuSection(2, "🌐", "Интернет", "Интернет жана пакеттер",
                "Интернет и пакеты", (3, 2, 7, 10)),
    MenuSection(3, "📦", "Тариф", "Тариф жана жазылуулар",
                "Тариф и подписки", (5, 8, 15)),
    MenuSection(4, "📱", "SIM / номер", "SIM жана номер",
                "SIM и номер", (6, 13, 12)),
    MenuSection(5, "✈️", "Роуминг", "Роуминг жана чет өлкө",
                "Роуминг и заграница", (4,)),
    MenuSection(0, "🧑‍💼", "Оператор", "Тирүү оператор",
                "Живой оператор", (14,)),
)


# --------------------------------------------------------------------------
# Мелкие помощники
# --------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _bi(ky: str, ru: str) -> str:
    """Двуязычная подпись интерфейса в одну строку."""
    return f"{ky} · {ru}"


def _short(text: str, limit: int = BUTTON_LABEL_LENGTH) -> str:
    """Обрезать подпись кнопки, не ломая слово посередине."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit].rsplit(" ", 1)[0]
    return f"{cut or clean[:limit]}…"


def extract_ussd_codes(*texts: str) -> tuple[str, ...]:
    """USSD-коды из текста, без повторов и в порядке появления."""
    found: list[str] = []
    for text in texts:
        for code in USSD_RE.findall(text or ""):
            if code not in found:
                found.append(code)
    return tuple(found)


def section_by_number(number: int) -> MenuSection | None:
    """Раздел меню по его цифре."""
    for section in MENU_SECTIONS:
        if section.number == int(number):
            return section
    return None


def section_for_faq(faq_id: int) -> MenuSection | None:
    """Раздел, в котором лежит тема."""
    for section in MENU_SECTIONS:
        if faq_id in section.faq_ids:
            return section
    return None


def _language_button(language: str) -> Button:
    """Единственная кнопка переключения языка — на случай ошибки."""
    other = "ru" if language == "ky" else "ky"
    flag = "🇷🇺" if other == "ru" else "🇰🇬"
    return Button(f"{flag} {LANGUAGE_NAMES[other]}", f"lang:{other}")


def _menu_button() -> Button:
    """«Меню» одинаково читается на обоих языках, поэтому подпись одна."""
    return Button("☰ Меню", "menu")


def _operator_button() -> Button:
    """То же и с «оператором»: слово общее для ru и ky."""
    return Button("0 Оператор", "operator")


def _faq_buttons(entries: Iterable[FaqEntry], language: str,
                 start: int = 1) -> tuple[tuple[Button, ...], ...]:
    """По кнопке на тему: цифра плюс вопрос на языке пользователя."""
    rows: list[tuple[Button, ...]] = []
    for number, entry in enumerate(entries, start=start):
        label = f"{number}  {_short(entry.question(language))}"
        rows.append((Button(label, f"faq:{entry.id}"),))
    return tuple(rows)


# --------------------------------------------------------------------------
# USSD-меню
# --------------------------------------------------------------------------

def menu_screen(language: str = config.DEFAULT_LANGUAGE,
                pinned: bool = False) -> Screen:
    """Главное меню: пронумерованные разделы, двуязычно в одну строку.

    `pinned` — язык выбран кнопкой. Состояние переключателя видно
    строкой в меню, иначе выбор языка остаётся невидимым: сами разделы
    двуязычны и выглядят одинаково на обоих языках.
    """
    lines = [
        f"<b>{_bi('INAI Mobile колдоо кызматы', 'служба поддержки')}</b>",
        _bi("Санды басыңыз же суроону жөн эле жазыңыз",
            "нажмите цифру или просто напишите вопрос"),
        language_status(language, pinned),
        "",
    ]
    for section in MENU_SECTIONS:
        lines.append(f"{section.icon} <b>{section.number}</b>  "
                     f"{_esc(section.title)}")
    lines.append("")
    free_input = _bi("Суроону өз сөзүңүз менен жазсаңыз болот",
                     "вопрос можно написать своими словами")
    lines.append(f"<i>{free_input}</i>")

    rows: list[tuple[Button, ...]] = []
    buttons = [Button(f"{section.number}  {section.short}",
                      f"section:{section.number}")
               for section in MENU_SECTIONS if section.number != 0]
    for index in range(0, len(buttons), 2):
        rows.append(tuple(buttons[index:index + 2]))
    rows.append((_operator_button(), _language_button(language)))
    return Screen("\n".join(lines), tuple(rows))


def start_screen(language: str = config.DEFAULT_LANGUAGE,
                 pinned: bool = False) -> Screen:
    """Экран /start: короткое приветствие и то же USSD-меню."""
    menu = menu_screen(language, pinned=pinned)
    greeting = _bi("Саламатсызбы!", "Здравствуйте!")
    return Screen(f"{greeting}\n{menu.text}", menu.rows)


def section_screen(section: MenuSection, entries: Sequence[FaqEntry],
                   language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Экран раздела: вопросы раздела кнопками, по-прежнему двуязычно."""
    chosen = [entry for entry in entries if entry.id in section.faq_ids]
    chosen.sort(key=lambda entry: section.faq_ids.index(entry.id))
    other = "ru" if language == "ky" else "ky"

    lines = [f"{section.icon} <b>{section.number}  {_esc(section.title)}</b>", ""]
    for number, entry in enumerate(chosen, start=1):
        lines.append(f"<b>{number}</b>  {_esc(entry.question(language))}")
        lines.append(f"<i>{_esc(entry.question(other))}</i>")
    lines.append("")
    free_input = _bi("Же өз сөзүңүз менен жазыңыз",
                     "или напишите своими словами")
    lines.append(f"<i>{free_input}</i>")

    rows = list(_faq_buttons(chosen, language))
    rows.append((_menu_button(), _operator_button()))
    return Screen("\n".join(lines), tuple(rows))


# --------------------------------------------------------------------------
# Карточка ответа и светофор
# --------------------------------------------------------------------------

LEVEL_MARKS: Mapping[str, str] = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def ussd_block(codes: Sequence[str]) -> list[str]:
    """Моноширинный блок с кодами плюс подсказка про копирование."""
    if not codes:
        return []
    lines = [f"<code>{_esc(code)}</code>" for code in codes]
    hint = _bi("кодду басып көчүрүңүз", "нажмите на код, чтобы скопировать")
    lines.append(f"<i>{hint}</i>")
    return lines


def answer_card(reply: Reply) -> Screen:
    """Двуязычная карточка ответа с учётом светофора уверенности.

    🟢 — ответ сразу; 🟡 — ответ и «может быть, вы про это?» с кнопками;
    🔴 — ответа нет, только три похожих вопроса кнопками.
    """
    if reply.level == "red" or reply.entry is None:
        return not_found_card(reply)

    entry = reply.entry
    language = reply.lang if reply.lang in config.LANGUAGES else config.DEFAULT_LANGUAGE
    other = "ru" if language == "ky" else "ky"
    mark = LEVEL_MARKS[reply.level]

    lines = [f"{mark} <b>{_esc(entry.question(language))}</b>", ""]
    if reply.inherited_from_context:
        carried = _bi("мурунку тема боюнча", "по предыдущей теме")
        lines.insert(1, f"<i>↪ {carried}</i>")
    lines.append(_esc(entry.answer(language)))

    codes = extract_ussd_codes(entry.answer(language), entry.answer(other))
    if codes:
        lines.append("")
        lines.extend(ussd_block(codes))

    lines.append("")
    lines.append(f"<i>{LANGUAGE_NAMES[other]}: {_esc(entry.answer(other))}</i>")

    rows: list[tuple[Button, ...]] = []
    others = [item for item in reply.suggestion_entries if item.id != entry.id]
    if reply.level == "yellow" and others:
        lines.append("")
        maybe = _bi("Балким, бул жөнүндөбү?", "может быть, вы про это?")
        lines.append(f"🟡 <i>{maybe}</i>")
        rows.extend(_faq_buttons(others[:config.TOP_K - 1], language))

    rows.append((
        Button(_bi("✅ Түшүнүктүү", "Понятно"), "ack"),
        Button(_bi("✍️ Башка суроо", "Другой вопрос"), "another"),
    ))
    rows.append((_menu_button(), _language_button(language)))
    return Screen("\n".join(lines), tuple(rows))


def not_found_card(reply: Reply) -> Screen:
    """🔴 Ответа нет: честный отказ и три похожих вопроса кнопками."""
    language = reply.lang if reply.lang in config.LANGUAGES else config.DEFAULT_LANGUAGE
    suggestions = list(reply.suggestion_entries)[:config.TOP_K]

    lines = [f"🔴 <b>{_bi('Так жообун таппадым', 'Точного ответа не нашёл')}</b>", ""]
    if suggestions:
        lines.append(_bi("Окшош суроолордон тандаңыз",
                         "выберите похожий вопрос"))
        lines.append(_bi("же башкача жазыңыз", "или напишите иначе"))
        lines.append("")
        for number, entry in enumerate(suggestions, start=1):
            lines.append(f"<b>{number}</b>  {_esc(entry.question(language))}")
    else:
        lines.append(_bi("Менюдан бөлүмдү тандаңыз",
                         "выберите раздел в меню"))
        lines.append(_bi("же башкача жазыңыз", "или напишите иначе"))

    rows = list(_faq_buttons(suggestions, language))
    rows.append((_menu_button(), _operator_button()))
    return Screen("\n".join(lines), tuple(rows))


def acknowledged_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Короткий экран после кнопки «Понятно»."""
    text = (f"✅ <b>{_bi('Жакшы!', 'Отлично!')}</b>\n"
            + _bi("Дагы суроо болсо жазыңыз", "если будет ещё вопрос — напишите"))
    return Screen(text, ((_menu_button(), _operator_button()),))


def another_question_screen(language: str = config.DEFAULT_LANGUAGE,
                            pinned: bool = False) -> Screen:
    """Экран после кнопки «Другой вопрос»: контекст темы сброшен."""
    text = (f"✍️ <b>{_bi('Жаңы суроо', 'Новый вопрос')}</b>\n"
            + _bi("Суроону жазыңыз же менюдан тандаңыз",
                  "напишите вопрос или выберите раздел"))
    menu = menu_screen(language, pinned=pinned)
    return Screen(f"{text}\n\n{menu.text}", menu.rows)


def operator_screen(entry: FaqEntry | None,
                    language: str = config.DEFAULT_LANGUAGE,
                    reason: str | None = None) -> Screen:
    """Передача оператору: условия берём из faq.json, ничего не выдумываем.

    `reason` — код причины из конфига: видно и абоненту, и в /stats.
    """
    lines = [f"🧑‍💼 <b>{_bi('Оператор', 'Оператор')}</b>", ""]
    if reason:
        lines.insert(1, f"<i>{_esc(reason_label(reason))}</i>")
    if entry is not None:
        other = "ru" if language == "ky" else "ky"
        lines.append(_esc(entry.answer(language)))
        lines.append("")
        lines.append(f"<i>{LANGUAGE_NAMES[other]}: {_esc(entry.answer(other))}</i>")
    lines.append("")
    note = _bi("Кайрылууңуз операторго белгиленди",
               "обращение помечено для оператора")
    lines.append(f"<i>{note}</i>")
    return Screen("\n".join(lines), ((_menu_button(),),))


def help_screen(language: str = config.DEFAULT_LANGUAGE,
                pinned: bool = False) -> Screen:
    """Справка по командам."""
    lines = [
        f"<b>{_bi('Жардам', 'Помощь')}</b>",
        "",
        "/start — " + _bi("меню", "меню"),
        "/stats — " + _bi("жооп табылбаган суроолор", "неотвеченные вопросы"),
        "/reset — " + _bi("теманы жана тилди тазалоо",
                          "сбросить тему и выбранный язык"),
        "",
        language_status(language, pinned),
        "",
        _bi("Суроону кыргызча же орусча жазыңыз",
            "напишите вопрос по-кыргызски или по-русски"),
    ]
    return Screen("\n".join(lines), ((_menu_button(), _language_button(language)),))


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------

def stats_screen(snapshot: StatsSnapshot,
                 entries: Sequence[FaqEntry] = (),
                 language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Агрегат неотвеченных вопросов. Чужих персональных данных здесь нет."""
    titles = {entry.id: entry.question(language) for entry in entries}
    title = _bi("Жооп табылбаган суроолор", "Неотвеченные вопросы")
    lines = [f"📊 <b>{title}</b>", ""]

    if not snapshot.total:
        lines.append(_bi("Азырынча суроолор жок", "пока вопросов не было"))
        return Screen("\n".join(lines), ((_menu_button(),),))

    share = f"{snapshot.unanswered_share:.0%}"
    lines.append(f"{_bi('Баары', 'всего вопросов')}: <b>{snapshot.total}</b>")
    lines.append(f"{_bi('Жооп табылды', 'найден ответ')}: {snapshot.answered}")
    lines.append(f"{_bi('Табылган жок', 'без ответа')}: "
                 f"<b>{snapshot.unanswered}</b> ({share})")
    if snapshot.by_language:
        languages = " · ".join(f"{name} {count}"
                               for name, count in snapshot.by_language.items())
        lines.append(f"{_bi('Тилдер', 'языки')}: {languages}")

    if snapshot.nearest_topics:
        lines.append("")
        header = _bi("Жакын темалар", "чаще всего не находились темы")
        lines.append(f"<b>{header}</b>")
        for faq_id, count in snapshot.nearest_topics:
            title = titles.get(faq_id, f"FAQ {faq_id}")
            lines.append(f"  • {faq_id} · {_esc(_short(title, 48))} — {count}")

    if snapshot.examples:
        lines.append("")
        lines.append(f"<b>{_bi('Мисалдар', 'примеры формулировок')}</b>")
        for example in snapshot.examples:
            lines.append(f"  • «{_esc(example)}»")

    if snapshot.operator_reasons:
        lines.append("")
        header = _bi("Операторго өтүү", "передано оператору")
        lines.append(f"<b>{header}</b>: {snapshot.operator_total}")
        for reason, count in snapshot.operator_reasons:
            lines.append(f"  • {_esc(reason_label(reason))} — {count}")

    lines.append("")
    footer = _bi("Жалпы сандар, жеке маалыматсыз",
                 "только агрегат, без персональных данных")
    lines.append(f"<i>{footer}</i>")
    return Screen("\n".join(lines), ((_menu_button(),),))


def plain_text(screen: Screen) -> str:
    """Текст экрана без HTML-разметки — для консоли и тестов."""
    without_tags = re.sub(r"<[^>]+>", "", screen.text)
    return html.unescape(without_tags)


# --------------------------------------------------------------------------
# Правила поведения: bot_rules.json
# --------------------------------------------------------------------------

#: Файл с текстами, списками слов и порогами. Лежит рядом с кодом, чтобы
#: его правил человек, а не разработчик.
RULES_PATH: Path = Path(__file__).with_name("bot_rules.json")

#: Встроенные значения на случай, если файла нет или он испорчен. Бот
#: обязан работать и без конфига: падать из-за текста приветствия нельзя.
DEFAULT_RULES: Mapping[str, Any] = {
    "operator": {
        "faq_id": 14,
        "direct_words": ("оператор", "operator", "опиратор", "аператор",
                         "живой человек", "менеджер", "тирүү оператор"),
        "escalation_words": ("жалоб", "претензи", "арыз", "возврат",
                             "обман", "мошенн", "украл"),
        "reason_labels": {
            "direct_request": "🙋 Попросили оператора",
            "complaint": "⚠️ Жалоба или претензия",
            "refund": "💸 Возврат средств",
            "out_of_base": "❓ Вопрос вне базы знаний",
            "repeated_miss": "🔁 Бот не понял несколько раз подряд",
        },
        "repeated_miss_threshold": 3,
    },
    "detection": {
        "greeting": ("салам", "саламатсыз", "ассалам", "привет",
                     "здравствуй", "здравствуйте", "добрый день",
                     "salam", "privet", "hello", "hi"),
        "about_bot": ("кто ты", "ты кто", "что умеешь", "как ты работаешь",
                      "ты бот", "сен кимсиң", "эмне кыла аласың"),
        "thanks": ("спасибо", "спс", "рахмат", "thanks"),
        "farewell": ("пока", "до свидания", "кош", "bye"),
    },
    "limits": {
        "max_question_chars": 1000,
        "replies_per_window": 5,
        "window_seconds": 10,
        "merge_window_seconds": 2.0,
    },
    "messages": {
        "greeting": {
            "ru": "Здравствуйте! Я справочная INAI Mobile. Что случилось?",
            "ky": "Саламатсызбы! Мен INAI Mobile маалымат кызматымын. "
                  "Эмне болду?",
        },
        "about_bot": {
            "ru": "Я справочная INAI Mobile и отвечаю по 15 темам. "
                  "Ответы беру из базы оператора, своего не придумываю.",
            "ky": "Мен INAI Mobile маалымат кызматымын, 15 тема боюнча "
                  "жооп берем. Жоопторду базадан алам, ойлоп чыгарбайм.",
        },
        "thanks": {
            "ru": "Пожалуйста! Обращайтесь, если что-то ещё понадобится.",
            "ky": "Арзыбайт! Дагы бир нерсе керек болсо, жазыңыз.",
        },
        "farewell": {
            "ru": "Всего доброго! Хорошей связи.",
            "ky": "Жакшы калыңыз! Байланышыңыз жакшы болсун.",
        },
        "too_long": {
            "ru": "Сообщение слишком длинное. Напишите вопрос одной фразой.",
            "ky": "Билдирүү өтө узун. Сурооңузду бир сүйлөм менен жазыңыз.",
        },
        "too_fast": {
            "ru": "Слишком много сообщений подряд. Подождите несколько "
                  "секунд и напишите вопрос одной фразой.",
            "ky": "Удаа көп билдирүү келди. Бир нече секунд күтүп, "
                  "сурооңузду бир сүйлөм менен жазыңыз.",
        },
        "repeated_miss": {
            "ru": "Похоже, я не понимаю вопрос. Давайте позову оператора.",
            "ky": "Сурооңузду түшүнбөй жатам окшойт. Операторду чакырайын.",
        },
        "language_switched": {
            "ru": "Готово, отвечаю по-русски. Переключить обратно можно "
                  "кнопкой.",
            "ky": "Болду, кыргызча жооп берем. Баскыч аркылуу кайра "
                  "которсоңуз болот.",
        },
        "service_unavailable": {
            "ru": "Справочная временно недоступна. Попробуйте через минуту "
                  "или напишите «оператор».",
            "ky": "Маалымат кызматы убактылуу иштебей жатат. Бир мүнөттөн "
                  "кийин аракет кылыңыз же «оператор» деп жазыңыз.",
        },
    },
}


@dataclass(frozen=True)
class Limits:
    """Пороги защиты от флуда (раздел «limits» конфига)."""

    max_question_chars: int
    replies_per_window: int
    window_seconds: float
    merge_window_seconds: float


@dataclass(frozen=True)
class OperatorRules:
    """Передача оператору (раздел «operator» конфига)."""

    faq_id: int
    direct_words: tuple[str, ...]
    escalation_words: tuple[str, ...]
    reason_labels: Mapping[str, str]
    repeated_miss_threshold: int


@dataclass(frozen=True)
class BotRules:
    """Проверенные правила поведения бота.

    Поле `source` говорит, откуда взяты значения: имя файла или
    «встроенные значения». Видно в логе запуска и на защите.
    """

    operator: OperatorRules
    detection: Mapping[str, tuple[str, ...]]
    messages: Mapping[str, Mapping[str, str]]
    limits: Limits
    source: str


def _visible(value: Any) -> Any:
    """Убрать служебные ключи вида `_doc`, не меняя сам файл.

    Ключи с подчёркиванием — документация для человека. Код их читать
    не обязан, но и удалять из конфига не имеет права.
    """
    if isinstance(value, Mapping):
        return {key: _visible(item) for key, item in value.items()
                if not str(key).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_visible(item) for item in value]
    return value


def _word_list(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Список слов из конфига: только непустые строки."""
    if not isinstance(value, (list, tuple)):
        return tuple(fallback)
    words = tuple(item.strip() for item in value
                  if isinstance(item, str) and item.strip())
    return words or tuple(fallback)


def _positive(value: Any, fallback: float) -> float:
    """Положительное число из конфига или значение по умолчанию."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number > 0 else float(fallback)


def _labels(value: Any, fallback: Mapping[str, str]) -> Mapping[str, str]:
    """Подписи причин: значения менять можно, ключи обязательны."""
    merged = dict(fallback)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str) and item.strip():
                merged[str(key)] = item.strip()
    return merged


def _message_pack(value: Any,
                  fallback: Mapping[str, Mapping[str, str]],
                  source: str) -> Mapping[str, Mapping[str, str]]:
    """Тексты сообщений: у каждого обязаны быть и ru, и ky.

    Неполный шаблон молча не принимаем: один забытый перевод оставил бы
    половину аудитории без ответа. Берём встроенный текст и пишем
    предупреждение в лог.
    """
    merged: dict[str, Mapping[str, str]] = {
        key: dict(item) for key, item in fallback.items()
    }
    if not isinstance(value, Mapping):
        return merged
    for key, item in value.items():
        name = str(key)
        if not isinstance(item, Mapping):
            logger.warning("%s: сообщение %s не словарь", source, name)
            continue
        texts = {language: str(item.get(language, "")).strip()
                 for language in config.LANGUAGES}
        if all(texts.values()):
            merged[name] = texts
            continue
        missing = ", ".join(language for language, text in texts.items()
                            if not text)
        logger.warning("%s: в сообщении %s нет перевода (%s)",
                       source, name, missing)
        merged.setdefault(name, {key: text for key, text in texts.items()
                                 if text})
    return merged


def _build_rules(raw: Any, source: str) -> BotRules:
    """Собрать правила из словаря конфига, подставив значения по умолчанию."""
    data = _visible(raw) if isinstance(raw, Mapping) else {}
    operator_data = data.get("operator")
    operator_data = operator_data if isinstance(operator_data, Mapping) else {}
    default_operator = DEFAULT_RULES["operator"]
    operator = OperatorRules(
        faq_id=int(_positive(operator_data.get("faq_id"),
                             default_operator["faq_id"])),
        direct_words=_word_list(operator_data.get("direct_words"),
                                default_operator["direct_words"]),
        escalation_words=_word_list(operator_data.get("escalation_words"),
                                    default_operator["escalation_words"]),
        reason_labels=_labels(operator_data.get("reason_labels"),
                              default_operator["reason_labels"]),
        repeated_miss_threshold=int(_positive(
            operator_data.get("repeated_miss_threshold"),
            default_operator["repeated_miss_threshold"])),
    )

    detection_data = data.get("detection")
    detection_data = detection_data if isinstance(detection_data, Mapping) else {}
    detection = {
        kind: _word_list(detection_data.get(kind), words)
        for kind, words in DEFAULT_RULES["detection"].items()
    }

    limits_data = data.get("limits")
    limits_data = limits_data if isinstance(limits_data, Mapping) else {}
    default_limits = DEFAULT_RULES["limits"]
    limits = Limits(
        max_question_chars=int(_positive(
            limits_data.get("max_question_chars"),
            default_limits["max_question_chars"])),
        replies_per_window=int(_positive(
            limits_data.get("replies_per_window"),
            default_limits["replies_per_window"])),
        window_seconds=_positive(limits_data.get("window_seconds"),
                                 default_limits["window_seconds"]),
        merge_window_seconds=_positive(
            limits_data.get("merge_window_seconds"),
            default_limits["merge_window_seconds"]),
    )

    return BotRules(
        operator=operator,
        detection=detection,
        messages=_message_pack(data.get("messages"),
                               DEFAULT_RULES["messages"], source),
        limits=limits,
        source=source,
    )


_rules_cache: BotRules | None = None


def rules(path: Path | None = None) -> BotRules:
    """Правила поведения бота: читаются один раз и кэшируются.

    Ни отсутствие `bot_rules.json`, ни битый JSON бота не ронят: в лог
    уходит понятное предупреждение, а поведение берётся из
    :data:`DEFAULT_RULES`. Явный `path` кэш не трогает — это для тестов.
    """
    global _rules_cache
    if path is None and _rules_cache is not None:
        return _rules_cache

    source = path or RULES_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("нет файла %s, работают встроенные правила", source)
        built = _build_rules({}, "встроенные значения")
    except ValueError as error:
        logger.warning("%s испорчен (%s), работают встроенные правила",
                       source, error)
        built = _build_rules({}, "встроенные значения")
    except OSError:
        logger.exception("не прочитан %s, работают встроенные правила", source)
        built = _build_rules({}, "встроенные значения")
    else:
        built = _build_rules(raw, source.name)

    if path is None:
        _rules_cache = built
    return built


def reload_rules() -> None:
    """Забыть прочитанный конфиг (тесты, правка файла на ходу)."""
    global _rules_cache
    _rules_cache = None


def message(name: str, language: str = config.DEFAULT_LANGUAGE) -> str:
    """Текст сообщения из конфига на нужном языке."""
    texts = rules().messages.get(name, {})
    if language not in config.LANGUAGES:
        language = config.DEFAULT_LANGUAGE
    return texts.get(language) or texts.get(config.DEFAULT_LANGUAGE, "")


def reason_label(reason: str) -> str:
    """Подпись причины передачи оператору (карточка и /stats)."""
    return rules().operator.reason_labels.get(reason, reason)


# --------------------------------------------------------------------------
# Реплики, которые не являются вопросами по базе
# --------------------------------------------------------------------------

#: Порядок проверки — от частного к общему: «привет, кто ты» это вопрос
#: о боте, а не просто приветствие.
INTENT_KINDS: tuple[str, ...] = ("about_bot", "thanks", "farewell",
                                 "greeting")

#: Приветствие и болтовня — короткая реплика. Длинная означает, что
#: внутри есть настоящий вопрос, и его разбирает `answer()`.
INTENT_MAX_WORDS: int = 6

#: Слова не длиннее этого сравниваются целиком. Иначе «кош» (прощание)
#: поймало бы «кошом» из «роумингди кантип кошом».
EXACT_MATCH_LENGTH: int = 4

#: Слова списка эскалации, из которых следует причина «возврат средств».
REFUND_HINTS: tuple[str, ...] = ("возврат", "кайтар", "верните")

INTENT_ICONS: Mapping[str, str] = {
    "greeting": "👋",
    "about_bot": "🤖",
    "thanks": "🙏",
    "farewell": "👋",
}


def matches_pattern(text: str, pattern: str) -> bool:
    """Встречается ли слово или фраза из конфига в реплике.

    Сравнение идёт по сглаженной форме (`normalize` плюс ң→н, ө→о, ү→у),
    поэтому «тируу оператор» и «тирүү оператор» — одно и то же. Слово
    длиннее :data:`EXACT_MATCH_LENGTH` сравнивается по началу: «салам»
    ловит «саламатсызбы», «жалоб» — «жалобу».
    """
    needle = fold(pattern)
    haystack = fold(text)
    if not needle or not haystack:
        return False
    if " " in needle:
        return f" {needle}" in f" {haystack} "
    words = haystack.split()
    if len(needle) <= EXACT_MATCH_LENGTH:
        return needle in words
    return any(word.startswith(needle) for word in words)


def detect_intent(text: str) -> str | None:
    """Приветствие, вопрос о боте, благодарность, прощание — или None.

    Ветка живёт только в слое бота и срабатывает только на короткой
    реплике без собственных ключевых слов темы: «салам» получает
    приветствие, «салам, интернет иштебейт» — обычный поиск по базе.
    Функция `answer()` об этой ветке не знает и по-прежнему отдаёт
    `faq_id = null`: так требует спецификация.
    """
    words = fold(text).split()
    if not words or len(words) > INTENT_MAX_WORDS:
        return None
    if mentions_topic(text):
        return None
    lists = rules().detection
    for kind in INTENT_KINDS:
        for pattern in lists.get(kind, ()):
            if matches_pattern(text, pattern):
                return kind
    return None


def operator_reason(text: str) -> str | None:
    """Причина передать диалог человеку или None.

    Прямая просьба («оператор») и ситуации, которые бот не имеет права
    разбирать сам: жалобы, претензии, возвраты, обвинения.
    """
    operator = rules().operator
    if not fold(text):
        return None
    for pattern in operator.direct_words:
        if matches_pattern(text, pattern):
            return "direct_request"
    for pattern in operator.escalation_words:
        if matches_pattern(text, pattern):
            folded = fold(pattern)
            if any(hint in folded for hint in REFUND_HINTS):
                return "refund"
            return "complaint"
    return None


# --------------------------------------------------------------------------
# Экраны для реплик вне базы и служебных уведомлений
# --------------------------------------------------------------------------

def bilingual_message(name: str,
                      language: str = config.DEFAULT_LANGUAGE) -> str:
    """Текст конфига: язык реплики крупно, второй язык курсивом.

    Так же устроена карточка ответа: абонент видит обе версии, даже если
    язык определился неверно.
    """
    if language not in config.LANGUAGES:
        language = config.DEFAULT_LANGUAGE
    other = "ru" if language == "ky" else "ky"
    main = message(name, language)
    second = message(name, other)
    lines = [_esc(main)]
    if second and second != main:
        lines.append("")
        lines.append(f"<i>{_esc(second)}</i>")
    return "\n".join(lines)


def smalltalk_screen(kind: str, language: str = config.DEFAULT_LANGUAGE,
                     pinned: bool = False) -> Screen:
    """Ответ на приветствие или болтовню вместо «ответа не нашёл».

    Приветствие и вопрос о боте идут вместе с меню: разговор только
    начался. Благодарность и прощание — коротко, меню не навязываем.
    """
    icon = INTENT_ICONS.get(kind, "💬")
    text = f"{icon} {bilingual_message(kind, language)}"
    if kind in ("greeting", "about_bot"):
        menu = menu_screen(language, pinned=pinned)
        return Screen(f"{text}\n\n{menu.text}", menu.rows)
    return Screen(text, ((_menu_button(), _operator_button()),))


def notice_screen(name: str, language: str = config.DEFAULT_LANGUAGE,
                  with_menu: bool = True) -> Screen:
    """Служебное уведомление: слишком длинно, слишком часто, не понял."""
    text = f"ℹ️ {bilingual_message(name, language)}"
    if not with_menu:
        return Screen(text)
    return Screen(text, ((_menu_button(), _operator_button()),))


def language_switched_screen(
        language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Подтверждение выбора языка кнопкой и меню с видимым состоянием."""
    menu = menu_screen(language, pinned=True)
    flag = "🇰🇬" if language == "ky" else "🇷🇺"
    head = f"{flag} <b>{_esc(message('language_switched', language))}</b>"
    return Screen(f"{head}\n\n{menu.text}", menu.rows)


def language_status(language: str = config.DEFAULT_LANGUAGE,
                    pinned: bool = False) -> str:
    """Строка состояния переключателя языка: что выбрано и каким образом."""
    name = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES["ru"])
    how = (_bi("баскыч менен", "выбран кнопкой") if pinned
           else _bi("автоматта", "определяется сам"))
    return f"🌐 {_bi('Жооп тили', 'язык ответов')}: <b>{name}</b> ({how})"


def with_operator_offer(screen: Screen,
                        language: str = config.DEFAULT_LANGUAGE,
                        reason: str = "repeated_miss") -> Screen:
    """Добавить к экрану предложение позвать оператора.

    Нужно после нескольких отказов подряд: абонента, которого не поняли
    трижды, нельзя заставлять самому искать кнопку.
    """
    label = reason_label(reason)
    text = (f"{screen.text}\n\n{_esc(label)}\n"
            f"{bilingual_message('repeated_miss', language)}")
    call = Button(_bi("🧑‍💼 Операторго", "Позвать оператора"), "operator")
    return Screen(text, tuple(screen.rows) + ((call,),), screen.parse_mode)
