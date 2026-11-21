"""Оформление ответов: USSD-меню, карточка ответа, светофор уверенности.

Слой намеренно отделён от Telegram: здесь только чистые функции, которые
возвращают текст и описание кнопок (`Screen`). Благодаря этому интерфейс
проверяется обычными тестами, а `bot.py` занимается лишь превращением
`Screen` в сообщение — причём на нажатие кнопки он **переписывает** то же
сообщение, а не присылает новое.

Четыре идеи, на которых держится интерфейс.

1. **Один язык за раз.** ТЗ требует «определяет язык вопроса, находит
   ответ и отвечает на том же языке», поэтому карточка, меню, списки тем
   и служебные подписи печатаются ровно на одном языке. Двуязычный текст
   вдвое длиннее, а переключатель языка при нём бессмыслен: что ни
   нажми, на экране всё равно оба языка. Единственное исключение —
   экран `/start`: там язык абонента ещё неизвестен.
2. **Переключатель перерисовывает экран.** Кнопка 🇰🇬/🇷🇺 несёт в себе
   адрес текущего экрана (`lang:ky:faq:4`), поэтому после нажатия абонент
   видит тот же ответ на другом языке, а не новое сообщение с меню.
3. **USSD-меню как вход.** Вся предметная область — это `*100#` и прочие
   короткие коды, абоненты к ним привыкли. Меню пронумеровано: цифра не
   имеет языка и работает одинаково для обеих аудиторий. Свободный ввод
   работает всегда, меню — запасной путь, а не обязательный шаг.
4. **Светофор уверенности.** 🟢 уверенный ответ, 🟡 ответ плюс «может
   быть, вы про это?», 🔴 ответа нет, только три похожих вопроса
   кнопками. Порог перестаёт быть числом в конфиге и становится видимым
   поведением.

Экрана «выберите язык» нет: язык определяется сам, переключатель спрятан
в одну кнопку, а результат нажатия виден по самому тексту экрана.

Полное меню печатается только там, где без него не обойтись: по `/start`,
по кнопке «Меню» и в отказе, к которому не нашлось даже похожих вопросов.
После удачного ответа меню не нужно — у карточки есть свои кнопки.

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
LANGUAGE_FLAGS: Mapping[str, str] = {"ru": "🇷🇺", "ky": "🇰🇬"}
BUTTON_LABEL_LENGTH = 38


@dataclass(frozen=True)
class Button:
    """Кнопка экрана.

    `action` — строка-команда для `bot.py`: `faq:<id>`, `menu`,
    `section:<n>`, `operator`, `ack:<id>`, `another:<id>`,
    `lang:<ru|ky>`, `stats`. Telegram здесь не упоминается намеренно.

    Номер темы едет внутри команды, потому что нажатие кнопки
    переписывает то сообщение, в котором эта кнопка: чтобы «Понятно» не
    стёрло ответ, а пересобрало ту же карточку, номер нужен прямо здесь,
    а не в памяти процесса.
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
    """Раздел USSD-меню: цифра, название на двух языках и темы FAQ."""

    number: int
    icon: str
    short: str
    title_ky: str
    title_ru: str
    faq_ids: tuple[int, ...]

    def name(self, language: str = config.DEFAULT_LANGUAGE) -> str:
        """Название раздела на одном языке — так печатается меню."""
        return self.title_ky if language == "ky" else self.title_ru

    @property
    def title(self) -> str:
        """Двуязычное название в одну строку. Только для экрана /start."""
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

_DASH = r"\s+—\s+"
_DASH_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_DASH + r"(?=(?:или|же)\b)"), " "),          # «… — или …»
    (re.compile(r"(?<=[\d#])" + _DASH), ": "),               # «1 — совсем не помог»
    (re.compile(r"(/\w+|ГБ|мин|SMS)" + _DASH), r"\1: "),     # «1 ГБ — 30 сом»
    (re.compile(_DASH), ", "),                               # два предложения
)


def plain_dashes(text: str) -> str:
    """Заменить длинное тире обычной пунктуацией.

    В чате длинное тире читается как книжный текст, а не как ответ
    справочной. Свои тексты мы пишем без него; эта функция нужна тому,
    что приходит со стороны: ответам из `faq.json` и формулировке модели.
    Смысл и числа не меняются, меняется только знак. Короткое тире в
    диапазонах («09:00–18:00») становится дефисом.
    """
    for pattern, replacement in _DASH_RULES:
        text = pattern.sub(replacement, text)
    return text.replace("—", "-").replace("–", "-")


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _t(language: str, ru: str, ky: str) -> str:
    """Служебная подпись на языке абонента: одна строка, без перевода.

    Весь интерфейс печатается через этот помощник. Дублировать служебные
    строки вторым языком нельзя: именно от этого экран раздувался вдвое,
    а кнопка переключения языка ничего видимого не меняла.
    """
    return ky if language == "ky" else ru


def _bi(ky: str, ru: str) -> str:
    """Двуязычная подпись в одну строку. Разрешена только на /start.

    На `/start` язык абонента ещё неизвестен: первая реплика не пришла,
    автоопределению нечего читать. Дальше язык известен — и дальше
    работает :func:`_t`.
    """
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


def other_language(language: str) -> str:
    """Второй язык интерфейса."""
    return "ru" if language == "ky" else "ky"


def _language_button(language: str, target: str = "menu") -> Button:
    """Кнопка переключения языка, которая перерисовывает текущий экран.

    `target` — адрес экрана, на котором стоит кнопка (`menu`, `help`,
    `faq:4`). `bot.screen_for_action()` сначала запоминает выбранный язык,
    потом снова собирает этот же экран. Поэтому нажатие 🇰🇬 на карточке
    даёт тот же ответ по-кыргызски, а не меню новым сообщением: иначе
    переключатель языка не виден в работе.
    """
    other = other_language(language)
    action = f"lang:{other}:{target}" if target else f"lang:{other}"
    return Button(f"{LANGUAGE_FLAGS[other]} {LANGUAGE_NAMES[other]}", action)


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
                bilingual: bool = False) -> Screen:
    """Главное меню: пронумерованные разделы на языке абонента.

    `bilingual` печатает названия разделов на двух языках и нужен ровно
    одному экрану — `/start`, где язык абонента ещё неизвестен.

    Строки «язык ответов: Русский (определяется сам)» здесь больше нет:
    она требовалась, пока смена языка ничего не меняла на экране. Теперь
    меню само написано на выбранном языке, и подпись на кнопке говорит,
    на какой язык переключит нажатие.
    """
    if bilingual:
        # /start: язык неизвестен, поэтому и подсказка на двух языках, но
        # покороче. Название бренда в переводе не нуждается.
        title = "INAI Mobile"
        hint = _bi("Санды басыңыз же суроону жазыңыз",
                   "нажмите цифру или напишите вопрос")
    else:
        title = _t(language, "INAI Mobile · служба поддержки",
                   "INAI Mobile · колдоо кызматы")
        hint = _t(language, "Нажмите цифру или напишите вопрос своими словами",
                  "Санды басыңыз же сурооңузду өз сөзүңүз менен жазыңыз")
    lines = [f"<b>{_esc(title)}</b>", f"<i>{_esc(hint)}</i>", ""]
    for section in MENU_SECTIONS:
        name = section.title if bilingual else section.name(language)
        lines.append(f"{section.icon} <b>{section.number}</b>  {_esc(name)}")

    rows: list[tuple[Button, ...]] = []
    buttons = [Button(f"{section.number}  {section.short}",
                      f"section:{section.number}")
               for section in MENU_SECTIONS if section.number != 0]
    for index in range(0, len(buttons), 2):
        rows.append(tuple(buttons[index:index + 2]))
    rows.append((_operator_button(), _language_button(language, "menu")))
    return Screen("\n".join(lines), tuple(rows))


def start_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Экран /start: приветствие и то же USSD-меню.

    Единственный двуязычный экран бота. Абонент ещё ничего не написал,
    определять язык не по чему, и встречать кыргызоязычного человека
    русским приветствием только потому, что `ru` стоит по умолчанию, —
    хуже, чем одна длинная строка.
    """
    menu = menu_screen(language, bilingual=True)
    greeting = _bi("Саламатсызбы!", "Здравствуйте!")
    return Screen(f"👋 <b>{greeting}</b>\n{menu.text}", menu.rows)


def section_screen(section: MenuSection, entries: Sequence[FaqEntry],
                   language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Экран раздела: темы раздела списком и кнопками, на одном языке.

    Раньше каждая тема печаталась дважды — вопрос на языке абонента и
    под ним перевод. Для раздела из четырёх тем это восемь строк вместо
    четырёх при той же пользе.
    """
    chosen = [entry for entry in entries if entry.id in section.faq_ids]
    chosen.sort(key=lambda entry: section.faq_ids.index(entry.id))

    lines = [f"{section.icon} <b>{section.number}  "
             f"{_esc(section.name(language))}</b>", ""]
    for number, entry in enumerate(chosen, start=1):
        lines.append(f"<b>{number}</b>  {_esc(entry.question(language))}")
    hint = _t(language, "или напишите своими словами",
              "же өз сөзүңүз менен жазыңыз")
    lines.append("")
    lines.append(f"<i>{_esc(hint)}</i>")

    rows = list(_faq_buttons(chosen, language))
    rows.append((_menu_button(), _operator_button()))
    return Screen("\n".join(lines), tuple(rows))


# --------------------------------------------------------------------------
# Карточка ответа и светофор
# --------------------------------------------------------------------------

LEVEL_MARKS: Mapping[str, str] = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def ussd_block(codes: Sequence[str],
               language: str = config.DEFAULT_LANGUAGE) -> list[str]:
    """Моноширинный блок с кодами плюс подсказка про копирование.

    Подсказка одна и на языке абонента: раньше она шла на двух языках и
    занимала в карточке столько же места, сколько сам ответ.
    """
    if not codes:
        return []
    hint = _t(language, "нажмите на код, чтобы скопировать",
              "кодду басып көчүрүңүз")
    lines = [f"<code>{_esc(code)}</code>" for code in codes]
    lines.append(f"<i>{_esc(hint)}</i>")
    return lines


def answer_card(reply: Reply, mark: str | None = None,
                note: str | None = None,
                rows: tuple[tuple[Button, ...], ...] | None = None,
                body: str | None = None) -> Screen:
    """Карточка ответа на языке абонента с учётом светофора уверенности.

    🟢 — ответ сразу; 🟡 — ответ и «может быть, вы про это?» с кнопками;
    🔴 — ответа нет, только три похожих вопроса кнопками.

    Второго перевода в карточке нет: ТЗ требует отвечать на языке
    вопроса, а не на двух сразу. Если язык определился неверно, абонент
    нажимает 🇰🇬/🇷🇺 — и эта же карточка перерисовывается на другом языке.

    `body` подменяет текст ответа переформулировкой из
    :mod:`faq_bot.rephrase`. Заголовок, светофор, блок USSD и кнопки
    остаются прежними: модели доверена только формулировка, а коды и
    цены по-прежнему берутся из записи базы.

    `mark`, `note` и `rows` нужны «закрытым» видам той же карточки —
    после «Понятно» и «Другого вопроса». Текст ответа там обязан
    остаться на экране, поэтому карточка собирается той же функцией, а не
    заменяется другим экраном: один источник истины, одна разметка.
    """
    if reply.level == "red" or reply.entry is None:
        return not_found_card(reply)

    entry = reply.entry
    language = (reply.lang if reply.lang in config.LANGUAGES
                else config.DEFAULT_LANGUAGE)
    other = other_language(language)
    mark = mark or LEVEL_MARKS[reply.level]

    lines = [f"{mark} <b>{_esc(entry.question(language))}</b>"]
    if reply.inherited_from_context:
        carried = _t(language, "по предыдущей теме", "мурунку тема боюнча")
        lines.append(f"<i>↪ {_esc(carried)}</i>")
    lines.append("")
    lines.append(_esc(plain_dashes(body if body else entry.answer(language))))

    # Код ищем в обоих переводах: в кыргызском тексте темы он иногда
    # записан, а в русском — нет, и наоборот.
    codes = extract_ussd_codes(entry.answer(language), entry.answer(other))
    if codes:
        lines.append("")
        lines.extend(ussd_block(codes, language))

    keyboard: list[tuple[Button, ...]] = []
    others = [item for item in reply.suggestion_entries if item.id != entry.id]
    if reply.level == "yellow" and others and rows is None:
        maybe = _t(language, "может быть, вы про это?",
                   "балким, бул жөнүндөбү?")
        lines.append("")
        lines.append(f"🟡 <i>{_esc(maybe)}</i>")
        keyboard.extend(_faq_buttons(others[:config.TOP_K - 1], language))

    if note:
        lines.append("")
        lines.append(f"<i>{_esc(note)}</i>")

    if rows is not None:
        return Screen("\n".join(lines), tuple(rows))

    # Адрес темы едет в самих кнопках: так состояние экрана хранится в
    # кнопке, а не в памяти процесса, и «Понятно» умеет пересобрать эту
    # же карточку, вместо того чтобы затереть её одной строкой.
    keyboard.append((
        Button(_t(language, "✅ Понятно", "✅ Түшүндүм"), f"ack:{entry.id}"),
        Button(_t(language, "✍️ Другой вопрос", "✍️ Башка суроо"),
               f"another:{entry.id}"),
    ))
    keyboard.append((_menu_button(),
                     _language_button(language, f"faq:{entry.id}")))
    return Screen("\n".join(lines), tuple(keyboard))


def not_found_card(reply: Reply) -> Screen:
    """🔴 Ответа нет: честный отказ и три похожих вопроса кнопками.

    Это один из трёх случаев, когда уместно полное меню, — но только
    если не нашлось даже похожих вопросов. Когда они есть, меню было бы
    шумом: выбирать нужно из них.
    """
    language = (reply.lang if reply.lang in config.LANGUAGES
                else config.DEFAULT_LANGUAGE)
    suggestions = list(reply.suggestion_entries)[:config.TOP_K]
    head = _t(language, "Точного ответа не нашёл", "Так жообун таппадым")

    lines = [f"🔴 <b>{_esc(head)}</b>"]
    if suggestions:
        hint = _t(language, "Выберите похожий вопрос или напишите иначе",
                  "Окшош суроону тандаңыз же башкача жазыңыз")
        lines.append(f"<i>{_esc(hint)}</i>")
        lines.append("")
        for number, entry in enumerate(suggestions, start=1):
            lines.append(f"<b>{number}</b>  {_esc(entry.question(language))}")
        rows = list(_faq_buttons(suggestions, language))
        rows.append((_menu_button(), _operator_button()))
        return Screen("\n".join(lines), tuple(rows))

    hint = _t(language, "Выберите раздел или напишите иначе",
              "Бөлүмдү тандаңыз же башкача жазыңыз")
    menu = menu_screen(language)
    lines.append(f"<i>{_esc(hint)}</i>")
    lines.append("")
    lines.append(menu.text)
    return Screen("\n".join(lines), menu.rows)


def acknowledged_card(reply: Reply) -> Screen:
    """«Понятно»: ответ остаётся на экране, уходит только клавиатура.

    Нажатие кнопки переписывает то сообщение, в котором эта кнопка, — и
    раньше «Понятно» затирало этим весь ответ. В живом тесте это
    выглядело так: вопрос «mende internet jok», а под ним единственная
    строка «✅ Болду». Ответ был, его стёрло случайное касание, и в
    истории переписки осталось, будто бот ответил «Болду» на вопрос про
    интернет.

    Поэтому «Понятно» ничего не стирает: текст темы и ответа остаётся
    дословно, 🟢 в заголовке меняется на ✅, клавиатура убирается
    (`reply_markup=None`), и внизу появляется одна строка подтверждения.
    Видно и сам ответ, и то, что вопрос закрыт.
    """
    language = (reply.lang if reply.lang in config.LANGUAGES
                else config.DEFAULT_LANGUAGE)
    note = _t(language, "Готово. Будет ещё вопрос, просто напишите.",
              "Болду. Дагы суроо болсо, жөн эле жазыңыз.")
    return answer_card(reply, mark="✅", note=note, rows=())


def acknowledged_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Подтверждение без карточки: кнопка пришла из старого сообщения.

    Запасной путь для `ack` без номера темы — например, из сообщения,
    отправленного прежней версией бота. Пересобрать карточку не из чего,
    поэтому остаётся одна строка.
    """
    text = _t(language, "✅ Готово. Будет ещё вопрос, просто напишите.",
              "✅ Болду. Дагы суроо болсо, жөн эле жазыңыз.")
    return Screen(text)


def another_question_card(reply: Reply) -> Screen:
    """«Другой вопрос»: ответ остаётся, добавляется подсказка.

    Та же причина, что и у «Понятно»: кнопка переписывает сообщение, и
    стирать ею готовый ответ нельзя. Абонент может передумать, вернуться
    к инструкции или просто перечитать её — текст остаётся на месте.
    Кнопки темы уходят, остаются меню и оператор.
    """
    language = (reply.lang if reply.lang in config.LANGUAGES
                else config.DEFAULT_LANGUAGE)
    note = _t(language,
              "✍️ Напишите новый вопрос своими словами, этот ответ "
              "останется здесь",
              "✍️ Жаңы сурооңузду өз сөзүңүз менен жазыңыз, бул жооп "
              "ордунда калат")
    return answer_card(reply, note=note,
                       rows=((_menu_button(), _operator_button()),))


def another_question_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Экран «новый вопрос» без карточки: /reset и кнопка из старого экрана.

    Полного меню здесь нет намеренно: абонент только что сказал, что
    хочет спросить своими словами. Меню — одной кнопкой.
    """
    head = _t(language, "Новый вопрос", "Жаңы суроо")
    hint = _t(language, "Напишите его своими словами или откройте меню",
              "Сурооңузду өз сөзүңүз менен жазыңыз же менюну ачыңыз")
    text = f"✍️ <b>{_esc(head)}</b>\n<i>{_esc(hint)}</i>"
    return Screen(text, ((_menu_button(), _operator_button()),))


#: Часы работы в ответе темы 14 записаны временем: «09:00–18:00».
CLOCK_RE = re.compile(r"\d{1,2}[:.]\d{2}")

#: Граница предложения в ответе из faq.json.
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

#: Глаголы инструкции «напишите „оператор“» на обоих языках. Сравнение
#: идёт по сглаженной форме, поэтому «жазы» ловит «жазыңыз».
INSTRUCTION_VERBS: tuple[str, ...] = ("напиш", "жазы", "write")


def _sentences(text: str) -> list[str]:
    """Ответ из базы по предложениям, без пустых."""
    return [part.strip() for part in SENTENCE_RE.split(text or "")
            if part.strip()]


def _is_self_instruction(sentence: str) -> bool:
    """Предложение вида «Напишите „оператор“ в боте»?

    Такое предложение объясняет, как ПОПРОСИТЬ оператора. Человеку,
    который уже попросил, оно бессмысленно, поэтому в подтверждении
    передачи его не цитируем.
    """
    folded = fold(sentence)
    if not any(verb in folded for verb in INSTRUCTION_VERBS):
        return False
    return any(fold(word) in folded
               for word in rules().operator.direct_words if fold(word))


def operator_facts(entry: FaqEntry | None,
                   language: str = config.DEFAULT_LANGUAGE) -> str:
    """Фактическая часть темы про поддержку: часы работы.

    Ответ темы 14 написан как ответ на вопрос «как связаться с
    поддержкой» и состоит из двух частей: инструкции («напишите
    „оператор“ в боте») и факта («работает ежедневно 09:00–18:00»).
    В подтверждении передачи нужен только факт.

    Факт разбирается из `faq.json`, а не переписан руками: берём те
    предложения ответа, в которых есть время. Если в базе времени нет
    вовсе, остаётся всё, кроме самой инструкции, — так правка ответа в
    `faq.json` не превращается в молчание бота.
    """
    if entry is None:
        return ""
    sentences = _sentences(entry.answer(language))
    hours = [item for item in sentences if CLOCK_RE.search(item)]
    if hours:
        return " ".join(hours)
    return " ".join(item for item in sentences
                    if not _is_self_instruction(item))


def operator_screen(entry: FaqEntry | None,
                    language: str = config.DEFAULT_LANGUAGE,
                    reason: str | None = None,
                    note: str | None = None) -> Screen:
    """Передача оператору либо ответ на вопрос «как связаться с поддержкой».

    `reason` — код причины из конфига: видно и абоненту, и в /stats.
    `note` — имя сообщения из конфига, которое объясняет передачу своими
    словами («больше советов по этой теме у меня нет»).

    Причина меняет смысл экрана, и это исправление живого теста. На слово
    «оператор» бот отвечал цитатой темы 14 целиком — то есть инструкцией
    «напишите „оператор“ в боте» человеку, который её только что
    выполнил. Поэтому:

    * **причина задана** (прямая просьба, жалоба, «совет не помог») —
      экран подтверждает передачу и оставляет из `faq.json` только факт:
      часы работы поддержки (:func:`operator_facts`);
    * **причины нет** — абонент СПРОСИЛ, как связаться с поддержкой, и
      тема 14 пришла обычным путём. Инструкция из базы здесь уместна и
      печатается целиком.

    Своих фактов экран не придумывает ни в том, ни в другом случае:
    часы работы разбираются из ответа темы, а не записаны в коде.
    """
    lines = ["🧑‍💼 <b>Оператор</b>"]
    if reason:
        lines.append(f"<i>{_esc(reason_label(reason, language))}</i>")
    if note:
        lines.append(_esc(message(note, language)))

    lines.append("")
    if reason:
        lines.append(_esc(message("operator_handoff", language)))
        facts = operator_facts(entry, language)
        if facts:
            lines.append(_esc(plain_dashes(facts)))
    elif entry is not None:
        lines.append(_esc(plain_dashes(entry.answer(language))))
    return Screen("\n".join(lines), ((_menu_button(),),))


def with_ticket_note(screen: Screen, ticket: str,
                     language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Дописать к экрану номер обращения: оператор его уже получил.

    Строка появляется только после того, как карточка обращения
    действительно ушла в операторский чат. Обещать ответ человека до
    этого нельзя: `OPERATOR_CHAT_ID` может быть не задан, а Telegram —
    не принять сообщение.
    """
    note = message("operator_queued", language)
    text = (f"{screen.text}\n\n🎫 № <code>{_esc(ticket)}</code>\n"
            f"<i>{_esc(note)}</i>")
    return Screen(text, screen.rows, screen.parse_mode)


#: До какой длины везём ответ оператора абоненту. У сообщения Telegram
#: предел 4096 знаков вместе с разметкой — оставляем запас.
OPERATOR_REPLY_LENGTH: int = 3000


def operator_answer_screen(text: str,
                           language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Ответ живого оператора в чате абонента, с явной пометкой.

    Пометка обязательна: иначе абонент не отличит слова человека от
    ответа из базы, а это разные обещания. Текст оператора печатается
    как есть (с экранированием), бот его не правит и не переводит.
    """
    body = (text or "").strip()
    if len(body) > OPERATOR_REPLY_LENGTH:
        body = body[:OPERATOR_REPLY_LENGTH].rstrip() + "…"
    title = message("operator_reply", language)
    return Screen(f"<b>{_esc(title)}</b>\n\n{_esc(body)}",
                  ((_menu_button(),),))


def help_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Справка по командам, на языке абонента."""
    head = _t(language, "Помощь", "Жардам")
    stats = _t(language, "неотвеченные вопросы", "жооп табылбаган суроолор")
    reset = _t(language, "сбросить тему и язык", "теманы жана тилди тазалоо")
    why = _t(language, "почему бот ответил так", "бот эмне үчүн ушундай жооп берди")
    hint = _t(language, "Вопрос можно писать своими словами",
              "Сурооңузду өз сөзүңүз менен жазсаңыз болот")
    lines = [
        f"<b>{_esc(head)}</b>",
        "",
        "/start: " + _esc(_t(language, "меню", "меню")),
        "/stats: " + _esc(stats),
        "/why: " + _esc(why),
        "/reset: " + _esc(reset),
        "",
        f"<i>{_esc(hint)}</i>",
    ]
    return Screen("\n".join(lines),
                  ((_menu_button(), _language_button(language, "help")),))


# --------------------------------------------------------------------------
# Статистика
# --------------------------------------------------------------------------

def stats_screen(snapshot: StatsSnapshot,
                 entries: Sequence[FaqEntry] = (),
                 language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Агрегат неотвеченных вопросов. Чужих персональных данных здесь нет."""
    titles = {entry.id: entry.question(language) for entry in entries}
    title = _t(language, "Неотвеченные вопросы", "Жооп табылбаган суроолор")
    lines = [f"📊 <b>{_esc(title)}</b>", ""]

    if not snapshot.total and not snapshot.operator_reasons:
        lines.append(_t(language, "пока вопросов не было",
                        "азырынча суроолор жок"))
        return Screen("\n".join(lines), ((_menu_button(),),))

    if snapshot.total:
        share = f"{snapshot.unanswered_share:.0%}"
        lines.append(f"{_t(language, 'всего вопросов', 'баары')}: "
                     f"<b>{snapshot.total}</b>")
        lines.append(f"{_t(language, 'найден ответ', 'жооп табылды')}: "
                     f"{snapshot.answered}")
        lines.append(f"{_t(language, 'без ответа', 'табылган жок')}: "
                     f"<b>{snapshot.unanswered}</b> ({share})")
    if snapshot.by_language:
        languages = " · ".join(f"{name} {count}"
                               for name, count in snapshot.by_language.items())
        lines.append(f"{_t(language, 'языки', 'тилдер')}: {languages}")

    if snapshot.ratings_total:
        # Оценку ставят сами абоненты после молчания; показываем её рядом
        # с долей неотвеченных, потому что вместе они и означают качество.
        label = _t(language, "оценка абонентов", "абоненттердин баасы")
        lines.append(f"{label}: <b>{snapshot.rating_average:.1f}</b> / 5 "
                     f"({snapshot.ratings_total})")

    if snapshot.nearest_topics:
        header = _t(language, "чаще всего не находились темы",
                    "эң көп табылбаган темалар")
        lines.append("")
        lines.append(f"<b>{_esc(header)}</b>")
        for faq_id, count in snapshot.nearest_topics:
            title = titles.get(faq_id, f"FAQ {faq_id}")
            lines.append(f"  • {faq_id} · {_esc(_short(title, 48))}: {count}")

    if snapshot.examples:
        header = _t(language, "примеры формулировок", "мисалдар")
        lines.append("")
        lines.append(f"<b>{_esc(header)}</b>")
        for example in snapshot.examples:
            lines.append(f"  • «{_esc(example)}»")

    if snapshot.operator_reasons:
        header = _t(language, "передано оператору", "операторго өттү")
        lines.append("")
        lines.append(f"<b>{_esc(header)}</b>: {snapshot.operator_total}")
        for reason, count in snapshot.operator_reasons:
            lines.append(f"  • {_esc(reason_label(reason, language))}: {count}")

    footer = _t(language, "только агрегат, без персональных данных",
                "жалпы сандар, жеке маалыматсыз")
    lines.append("")
    lines.append(f"<i>{_esc(footer)}</i>")
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
            "direct_request": {"ru": "🙋 Попросили оператора",
                               "ky": "🙋 Оператор сурадыңыз"},
            "complaint": {"ru": "⚠️ Жалоба или претензия",
                          "ky": "⚠️ Арыз же нааразычылык"},
            "refund": {"ru": "💸 Возврат средств",
                       "ky": "💸 Акчаны кайтаруу"},
            "out_of_base": {"ru": "❓ Вопрос вне базы знаний",
                            "ky": "❓ База сыртындагы суроо"},
            "repeated_miss": {"ru": "🔁 Бот не понял несколько раз подряд",
                              "ky": "🔁 Бот бир нече жолу түшүнбөдү"},
            "not_helping": {"ru": "🛠 Совет не помог",
                            "ky": "🛠 Кеңеш жардам бербеди"},
        },
        "repeated_miss_threshold": 3,
    },
    "refusal": {
        "phrases": ("не помог", "не помогло", "всё равно", "все равно",
                    "уже проверил", "уже сделал", "пробовал",
                    "не получается", "то же самое",
                    "баары бир", "жардам берген жок", "кылдым",
                    "текшердим", "иштебей эле жатат", "болбоду",
                    "дагы эле"),
        "repeat_window_seconds": 180.0,
        "escalate_after": 2,
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
        "revise_window_seconds": 9.0,
        "read_seconds": 3.0,
        "max_revisions": 6,
        "rating_after_seconds": 300.0,
        "rating_repeat_hours": 6.0,
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
        "not_helping": {
            "ru": "Понял, этот совет не помог. Тогда дело может быть "
                  "в другом.",
            "ky": "Түшүндүм, бул кеңеш жардам бербеди. Анда себеби башка "
                  "нерседе болушу мүмкүн.",
        },
        "maybe_this": {
            "ru": "Может быть, дело в этом:",
            "ky": "Балким, себеби ушунда:",
        },
        "no_more_advice": {
            "ru": "Больше советов по этой теме у меня нет, передаю "
                  "оператору.",
            "ky": "Бул тема боюнча менде башка кеңеш жок, операторго "
                  "өткөрөм.",
        },
        "revised": {
            "ru": "↻ учёл ваше уточнение",
            "ky": "↻ тактооңузду эске алдым",
        },
        "voice_listening": {
            "ru": "🎤 Слушаю запись…",
            "ky": "🎤 Жазууну угуп жатам…",
        },
        "voice_too_long": {
            "ru": "Запись слишком длинная. Скажите короче или напишите "
                  "текстом.",
            "ky": "Жазуу өтө узун. Кыскача айтыңыз же текст жазыңыз.",
        },
        "voice_rate_limited": {
            "ru": "Голосовых пока слишком много. Попробуйте позже или "
                  "напишите вопрос текстом.",
            "ky": "Үн билдирүүлөр азыр өтө көп болду. Кийинчерээк аракет "
                  "кылыңыз же текст жазыңыз.",
        },
        "voice_no_key": {
            "ru": "Распознавание голоса недоступно. Напишите вопрос "
                  "текстом, отвечу так же.",
            "ky": "Үндү тааныбай жатам. Сурооңузду текст менен жазыңыз, "
                  "ошол эле жообун берем.",
        },
        "voice_nothing": {
            "ru": "Не расслышал. Повторите, пожалуйста, или напишите "
                  "текстом.",
            "ky": "Укпай калдым. Кайталап айтыңыз же текст жазыңыз.",
        },
        "escalated": {
            "ru": "Такие обращения решает оператор. Инструкция выше, "
                  "обращение я помечаю для человека.",
            "ky": "Мындай кайрылууларды оператор чечет. Жогоруда көрсөтмө "
                  "бар, кайрылууңузду адамга белгиледим.",
        },
        "operator_handoff": {
            "ru": "Обращение принято и помечено для оператора.",
            "ky": "Кайрылуу кабыл алынды жана операторго белгиленди.",
        },
        "operator_queued": {
            "ru": "Оператор уже видит обращение, ответ придёт в этот же чат.",
            "ky": "Оператор кайрылууну көрүп турат, жооп ушул чатка келет.",
        },
        "operator_reply": {
            "ru": "🧑‍💼 Ответ оператора",
            "ky": "🧑‍💼 Оператордун жообу",
        },
        "operator_card_hint": {
            "ru": "Ответьте реплаем на это сообщение, бот передаст текст "
                  "абоненту.",
            "ky": "Бул билдирүүгө реплай менен жооп бериңиз, бот текстти "
                  "абонентке жеткирет.",
        },
        "operator_unknown": {
            "ru": "Не нахожу обращение для этого сообщения: бот "
                  "перезапускался. Попросите абонента написать снова.",
            "ky": "Бул билдирүүгө тиешелүү кайрылууну таппадым: бот кайра "
                  "жүктөлгөн. Абонентти кайра жазууга өтүнүңүз.",
        },
        "operator_delivered": {
            "ru": "✅ Ответ доставлен абоненту.",
            "ky": "✅ Жооп абонентке жеткирилди.",
        },
        "operator_failed": {
            "ru": "Не удалось доставить ответ: абонент мог остановить бота.",
            "ky": "Жоопту жеткире албадым: абонент ботту токтоткон болушу "
                  "мүмкүн.",
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
    """Пороги защиты от флуда и окна разговора (раздел «limits»).

    `revise_window_seconds` — окно дозрения ответа: дописанная в него
    реплика переписывает последний ответ бота, а не добавляет новый.
    `read_seconds` — за сколько абонент успевает прочитать ответ; по
    этому сроку отличается «дописал свою мысль» от «прочитал и говорит,
    что не помогло».
    """

    max_question_chars: int
    replies_per_window: int
    window_seconds: float
    revise_window_seconds: float
    read_seconds: float
    max_revisions: int
    #: Через сколько секунд молчания просить оценку работы.
    rating_after_seconds: float = 300.0
    #: Не чаще чем раз в столько часов спрашивать один и тот же чат.
    rating_repeat_hours: float = 6.0


@dataclass(frozen=True)
class RefusalRules:
    """Признаки отказа от предыдущего ответа (раздел «refusal»).

    `phrases` — явные слова отказа на обоих языках.
    `repeat_window_seconds` — окно, в котором повтор одной и той же темы
    сам по себе считается отказом.
    `escalate_after` — после какого числа отказов по теме звать человека.
    """

    phrases: tuple[str, ...]
    repeat_window_seconds: float
    escalate_after: int


@dataclass(frozen=True)
class OperatorRules:
    """Передача оператору (раздел «operator» конфига)."""

    faq_id: int
    direct_words: tuple[str, ...]
    escalation_words: tuple[str, ...]
    reason_labels: Mapping[str, Mapping[str, str]]
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
    refusal: RefusalRules
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


def _labels(value: Any, fallback: Mapping[str, Mapping[str, str]],
            ) -> Mapping[str, Mapping[str, str]]:
    """Подписи причин: значения менять можно, ключи обязательны.

    У подписи два перевода — экран печатается на одном языке, и
    кыргызская карточка не имеет права кончаться русской строкой. Одна
    строка вместо пары тоже принимается: тогда она идёт на оба языка.
    Так старый конфиг продолжает работать.
    """
    merged: dict[str, Mapping[str, str]] = {
        key: dict(item) for key, item in fallback.items()
    }
    if not isinstance(value, Mapping):
        return merged
    for key, item in value.items():
        name = str(key)
        if isinstance(item, str) and item.strip():
            merged[name] = {language: item.strip()
                            for language in config.LANGUAGES}
            continue
        if not isinstance(item, Mapping):
            continue
        texts = {language: str(item.get(language, "")).strip()
                 for language in config.LANGUAGES}
        known = dict(merged.get(name, {}))
        known.update({language: text for language, text in texts.items()
                      if text})
        if known:
            merged[name] = known
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
        revise_window_seconds=_positive(
            limits_data.get("revise_window_seconds"),
            default_limits["revise_window_seconds"]),
        read_seconds=_positive(limits_data.get("read_seconds"),
                               default_limits["read_seconds"]),
        max_revisions=int(_positive(limits_data.get("max_revisions"),
                                    default_limits["max_revisions"])),
        rating_after_seconds=_positive(
            limits_data.get("rating_after_seconds"),
            default_limits["rating_after_seconds"]),
        rating_repeat_hours=_positive(
            limits_data.get("rating_repeat_hours"),
            default_limits["rating_repeat_hours"]),
    )

    refusal_data = data.get("refusal")
    refusal_data = refusal_data if isinstance(refusal_data, Mapping) else {}
    default_refusal = DEFAULT_RULES["refusal"]
    refusal = RefusalRules(
        phrases=_word_list(refusal_data.get("phrases"),
                           default_refusal["phrases"]),
        repeat_window_seconds=_positive(
            refusal_data.get("repeat_window_seconds"),
            default_refusal["repeat_window_seconds"]),
        escalate_after=int(_positive(refusal_data.get("escalate_after"),
                                     default_refusal["escalate_after"])),
    )

    return BotRules(
        operator=operator,
        detection=detection,
        messages=_message_pack(data.get("messages"),
                               DEFAULT_RULES["messages"], source),
        limits=limits,
        refusal=refusal,
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


def reason_label(reason: str,
                 language: str = config.DEFAULT_LANGUAGE) -> str:
    """Подпись причины передачи оператору (карточка и /stats)."""
    texts = rules().operator.reason_labels.get(reason, {})
    if language not in config.LANGUAGES:
        language = config.DEFAULT_LANGUAGE
    return (texts.get(language) or texts.get(config.DEFAULT_LANGUAGE)
            or reason)


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


def _without_pattern(text: str, pattern: str) -> str:
    """Что осталось от реплики, если вычеркнуть совпавшую фразу.

    Остаток проверяется на ключевые слова темы, поэтому вычёркиваем по
    исходным словам, а не по сглаженной строке: буквы ң, ө, ү нужны,
    чтобы узнать «мүнөт» и «бөгөт» в остатке.
    """
    needle = fold(pattern)
    needle_words = set(needle.split())
    kept: list[str] = []
    for word in text.split():
        folded_word = fold(word)
        if not folded_word or folded_word in needle_words:
            continue
        if (" " not in needle and len(needle) > EXACT_MATCH_LENGTH
                and folded_word.startswith(needle)):
            continue
        kept.append(word)
    return " ".join(kept)


def detect_intent(text: str) -> str | None:
    """Приветствие, вопрос о боте, благодарность, прощание — или None.

    Ветка живёт только в слое бота и срабатывает только на короткой
    реплике, в которой, кроме самой болтовни, нет ключевых слов темы:
    «салам» получает приветствие, «салам, интернет иштебейт» — обычный
    поиск по базе. Проверяется именно остаток реплики: «добрый день»
    целиком является приветствием, хотя слово «день» похоже на начало
    тематического «деньги».

    Функция `answer()` об этой ветке не знает и по-прежнему отдаёт
    `faq_id = null`: так требует спецификация.
    """
    words = fold(text).split()
    if not words or len(words) > INTENT_MAX_WORDS:
        return None
    lists = rules().detection
    for kind in INTENT_KINDS:
        for pattern in lists.get(kind, ()):
            if not matches_pattern(text, pattern):
                continue
            if mentions_topic(_without_pattern(text, pattern)):
                return None
            return kind
    return None


def says_refusal(text: str) -> bool:
    """Сказал ли абонент, что предыдущий ответ не помог.

    Только слова, без состояния диалога: решение «это отказ» принимает
    слой бота, потому что одних слов мало. «Не помогло, а как пополнить
    счёт?» — это новый вопрос, и отказом он становится лишь тогда, когда
    новый поиск не нашёл другую тему увереннее.

    Повод для функции — живой тест на кыргызском: на «гб бар экен, бирок
    баары бир иштебей жатат» бот дословно повторил предыдущий ответ.
    """
    if not fold(text):
        return False
    return any(matches_pattern(text, pattern)
               for pattern in rules().refusal.phrases)


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

def smalltalk_screen(kind: str,
                     language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Ответ на приветствие или болтовню вместо «ответа не нашёл».

    Полного меню здесь больше нет: на «салам» приходило приветствие
    и сразу шесть пунктов меню, то есть восемнадцать строк на одно
    слово. Меню — одной кнопкой, а язык приветствия уже известен: его
    определили по самой реплике.
    """
    icon = INTENT_ICONS.get(kind, "💬")
    text = f"{icon} {_esc(message(kind, language))}"
    return Screen(text, ((_menu_button(), _operator_button()),))


def notice_screen(name: str, language: str = config.DEFAULT_LANGUAGE,
                  with_menu: bool = True) -> Screen:
    """Служебное уведомление: слишком длинно, слишком часто, не понял."""
    text = f"ℹ️ {_esc(message(name, language))}"
    if not with_menu:
        return Screen(text)
    return Screen(text, ((_menu_button(), _operator_button()),))


#: Возможные оценки. Пять делений, как привычная школьная шкала.
RATING_SCORES: tuple[int, ...] = (1, 2, 3, 4, 5)

#: Ниже этой оценки к благодарности добавляется предложение оператора.
RATING_LOW: int = 2


def rating_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Просьба оценить работу после долгого молчания.

    Кнопки — голые цифры, и это то же решение, что у USSD-меню (D-06):
    цифра не имеет языка. Её нажмёт и тот, кто плохо читает по-русски,
    и тот, кто по-кыргызски, и ошибиться в ней нельзя.

    Шкала подписана прямо в тексте — «1 совсем не помог, 5 всё решил», —
    потому что без подписи пять кнопок читаются как номера тем, а
    номерами тем в этом боте и правда пользуются.
    """
    row = tuple(Button(str(score), f"rate:{score}") for score in RATING_SCORES)
    return Screen(f"⭐ {_esc(message('rating_ask', language))}", (row,))


def rating_done_screen(score: int,
                       language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Экран после нажатия: оценка видна, кнопок больше нет.

    Карточка не исчезает и не превращается в новое сообщение — она
    перерисовывается на месте, как и везде в этом боте. Оценка остаётся
    на экране: иначе непонятно, засчиталось ли нажатие.

    На низкой оценке благодарности мало. Человек сказал, что мы не
    помогли; ответ «спасибо» без предложения помощи — это тупик.
    """
    stars = "⭐" * max(1, min(score, len(RATING_SCORES)))
    lines = [f"{stars} <b>{score} / {len(RATING_SCORES)}</b>",
             "",
             _esc(message("rating_thanks", language))]
    if score <= RATING_LOW:
        lines.append("")
        lines.append(f"<i>{_esc(message('rating_low_hint', language))}</i>")
    return Screen("\n".join(lines))


#: До какой длины показывать распознанную речь. Эхо нужно, чтобы абонент
#: увидел, что именно услышал бот, а не чтобы пересказать запись целиком.
HEARD_LENGTH: int = 160


def listening_screen(language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """«Слушаю запись…» — ответ на голосовое, пока идёт распознавание.

    Это сообщение потом переписывается самим ответом, поэтому голосовое
    добавляет в чат ровно одно сообщение, как и текстовый вопрос.
    Кнопок нет намеренно: нажимать во время распознавания нечего.
    """
    return Screen(_esc(message("voice_listening", language)))


def with_heard(screen: Screen, heard: str,
               language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Эхо распознанной речи перед ответом.

    Распознавание ошибается, особенно на кыргызском и в шуме. Без эха
    абонент не поймёт, почему ответ мимо: он сказал «роуминг», а бот
    услышал «пополнить». Одна строка сверху показывает услышанное — и
    дальше идёт обычная карточка, без всякой особой логики для голоса.
    """
    shown = _short(heard, HEARD_LENGTH)
    return Screen(f"🎤 <i>«{_esc(shown)}»</i>\n\n{screen.text}",
                  screen.rows, screen.parse_mode)


def language_switched_screen(
        language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Меню на выбранном языке плюс строка подтверждения.

    Нужен там, где переключатель нажали на самом меню: сравнивать не с
    чем, поэтому одна строка говорит, что произошло. На карточке ответа
    подтверждение не нужно — там виден тот же ответ на другом языке.
    """
    menu = menu_screen(language)
    flag = LANGUAGE_FLAGS.get(language, "🌐")
    head = f"{flag} <b>{_esc(message('language_switched', language))}</b>"
    return Screen(f"{head}\n{menu.text}", menu.rows)


def alternatives_screen(entries: Sequence[FaqEntry],
                        language: str = config.DEFAULT_LANGUAGE,
                        rejected: FaqEntry | None = None) -> Screen:
    """Другие темы вместо повтора ответа, который не помог.

    Экран сознательно не содержит текста отвергнутой темы: абонент
    только что сказал, что этот совет не сработал, и видеть его снова —
    худшее, что может сделать бот. Вместо него — честное признание,
    название темы, которая не помогла, и до трёх других тем кнопками
    плюс живой оператор.
    """
    if language not in config.LANGUAGES:
        language = config.DEFAULT_LANGUAGE
    lines = [f"🔁 <b>{_esc(message('not_helping', language))}</b>"]
    if rejected is not None:
        tried = _t(language, "не помогло", "жардам бербеди")
        lines.append(f"<i>✗ {_esc(rejected.question(language))}, "
                     f"{_esc(tried)}</i>")

    picked = list(entries)[:config.TOP_K]
    rows: list[tuple[Button, ...]] = []
    if picked:
        lines.append("")
        lines.append(f"<i>{_esc(message('maybe_this', language))}</i>")
        for number, entry in enumerate(picked, start=1):
            lines.append(f"<b>{number}</b>  {_esc(entry.question(language))}")
        rows.extend(_faq_buttons(picked, language))
    else:
        hint = _t(language, "Опишите, что именно не получается",
                  "Эмне болбой жатканын жазыңыз")
        lines.append("")
        lines.append(f"<i>{_esc(hint)}</i>")

    rows.append((_menu_button(), _operator_button()))
    return Screen("\n".join(lines), tuple(rows))


def with_revision_note(screen: Screen,
                       language: str = config.DEFAULT_LANGUAGE) -> Screen:
    """Пометить ответ, пересчитанный по дописанной реплике.

    Правка сообщения, которое уже уехало вверх по переписке, незаметна:
    Telegram показывает лишь мелкую метку «изменено». Одна строка внизу
    говорит, что бот учёл дописанное, — и заодно гарантирует, что текст
    действительно изменился, иначе Telegram отклонил бы правку.
    """
    note = message("revised", language)
    return Screen(f"{screen.text}\n\n<i>{_esc(note)}</i>",
                  screen.rows, screen.parse_mode)


def with_operator_offer(screen: Screen,
                        language: str = config.DEFAULT_LANGUAGE,
                        reason: str = "repeated_miss",
                        note: str = "repeated_miss") -> Screen:
    """Добавить к экрану причину и предложение позвать оператора.

    Нужно в двух случаях: бот не понял несколько раз подряд, или ответ
    из базы нашёлся, но ситуация всё равно для человека — жалоба,
    возврат, кража. Во втором случае абонент получает и инструкцию из
    `faq.json`, и передачу оператору, а не одно вместо другого.
    """
    label = reason_label(reason, language)
    text = (f"{screen.text}\n\n{_esc(label)}\n"
            f"<i>{_esc(message(note, language))}</i>")
    call = Button(_t(language, "🧑‍💼 Позвать оператора", "🧑‍💼 Операторго"),
                  "operator")
    return Screen(text, tuple(screen.rows) + ((call,),), screen.parse_mode)
