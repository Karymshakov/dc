"""Переформулировка ответа из faq.json под вопрос абонента. Бонус ТЗ.

Зачем это нужно
---------------

Бот цитирует `faq.json` дословно, и это правильно: выдумать факт он так
не может (D-09). Но дословная цитата иногда отвечает не на тот вопрос,
который задан. Живой случай: абонент пишет «оператор» — то есть уже
выполнил инструкцию, — а бот отвечает «Напишите «оператор» в боте».
Инструкция верна и бесполезна: её адресат её уже выполнил.

Модуль решает ровно эту задачу: берёт **текст темы из базы** и
переписывает его под формулировку вопроса, **не добавляя ни одного
факта**. Это и есть бонусный пункт «LLM с RAG по FAQ без выдумки
фактов»: retrieval остаётся нашим бесплатным правиловым движком,
модель получает только найденную запись и не имеет права выйти за неё.

Почему модели нельзя верить на слово
------------------------------------

Проверяет не обещание в промпте, а код после генерации. Три сторожа,
от главного к вспомогательным:

1. :func:`faq_bot.consistency.check` — факт из ответа обязан принадлежать
   ЭТОЙ записи базы. Главный случай из D-09: код `*111#` настоящий, но
   принадлежит доп. интернету, а не балансу. Абонент наберёт рабочую
   команду и получит не то — это хуже выдумки.
2. «Новых цифр не бывает». Сторож связей ловит USSD, цены и объёмы, но
   не часы работы и не проценты. Поэтому любая группа цифр из ответа
   модели обязана дословно встречаться в записи базы. Правило строгое
   намеренно: лишняя строгость стоит нам только отказа от улучшения,
   пропущенная цифра — доверия абонента.
3. Удвоение длины. Ответ справочной короток по определению; модель,
   которая разговорилась вдвое, почти наверняка что-то присочинила.

Плюс язык: переформулировка кыргызского ответа, съехавшая на русский,
нарушает требование ТЗ «отвечает на том же языке», поэтому язык
результата проверяется нашим же определителем.

Любое срабатывание сторожа означает одно и то же: абонент получает
**дословный текст из faq.json**. Худший исход улучшения — отсутствие
улучшения, а не неверный ответ.

Экономия — требование, а не пожелание
-------------------------------------

Ключ оплачивает человек (D-10), поэтому:

* модуль **выключен по умолчанию**: :data:`ENABLED` читается из
  `REPHRASE_ENABLED`. На автопроверке жюри идёт `answer()`, а не бот,
  и платить там не за что;
* модель самая дешёвая из линейки — `gemini-flash-lite-latest`;
* повторный вопрос не стоит ничего: результат кешируется по
  `(faq_id, язык, нормализованный вопрос)`;
* отклонённые сторожем ответы тоже кешируются: тот же вопрос даст то же
  нарушение, платить за него второй раз бессмысленно;
* бюджеты на чат, на час и на сутки — как в :mod:`faq_bot.speech`;
* `maxOutputTokens` ограничен, вопрос в промпте обрезается;
* всё, что можно отклонить без обращения к сети, отклоняется до него.

Функция :func:`rephrase` никогда не бросает исключение. Нет ключа, нет
сети, таймаут, мусор вместо JSON — абонент получает текст из базы.
Необязательное улучшение не имеет права уронить ответ.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import consistency
from .contracts import FaqEntry
from .lang import detect
from .normalize import normalize

# Ключ и разбор ответа Gemini берём из speech: ключ один, читать его двумя
# способами — это два места, где он однажды перестанет находиться.
from .speech import api_key, extract_text

logger = logging.getLogger(__name__)

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")
DEFAULT_MODEL = "gemini-flash-lite-latest"

# Абонент ждёт ответа прямо сейчас. Если модель не успела — отдаём базу:
# дословный ответ лучше, чем задумчивая пауза.
TIMEOUT_SECONDS = 15

# --- Пределы. Все до единого про чужие деньги ------------------------------

#: Потолок генерации. Ответ справочной — одна-две фразы; настоящим
#: ограничителем служит правило удвоения длины ниже, а это защита от
#: модели, решившей написать эссе за наш счёт.
MAX_OUTPUT_TOKENS = 256

#: Переписывание, а не сочинение: температура близка к нулю.
TEMPERATURE = 0.2

#: Длина вопроса в промпте. Платим за каждый токен, а смысл вопроса
#: укладывается в одну строку.
MAX_QUESTION_CHARS = 200

#: Во сколько раз ответ модели может быть длиннее исходного.
MAX_LENGTH_RATIO = 2.0

#: Короче этого — не ответ, а реплика: факты по дороге потерялись.
MIN_TEXT_CHARS = 12

#: Бюджеты в ВЫЗОВАХ модели (в speech они в секундах звука — там платят
#: за длительность, здесь за обращение).
#:
#: 20 вызовов на чат в час — это 20 РАЗНЫХ вопросов за час от одного
#: человека; повторы бесплатны из кеша. Живой диалог со справочной столько
#: не набирает. 200 в сутки на всего бота — это около 60 тысяч токенов,
#: то есть доли цента на flash-lite: предел защищает от сорвавшегося
#: цикла, а не от обычной работы.
CALLS_PER_CHAT_HOUR = 20
CALLS_TOTAL_HOUR = 60
CALLS_TOTAL_DAY = 200

#: Кеш живёт в памяти процесса и ограничен, чтобы долгий прогон бота
#: не превратил экономию в утечку.
CACHE_LIMIT = 512

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

#: Откуда взят показанный текст.
LLM = "llm"          # переформулировка модели, прошедшая всех сторожей
FAQ = "faq"          # модель ответила, ответ отклонён — текст из базы
SKIPPED = "skipped"  # к модели не обращались вовсе — текст из базы

LANGUAGE_NAMES = {"ky": "кыргызский", "ru": "русский"}

#: Группы цифр: основа правила «новых цифр не бывает».
DIGITS_PATTERN = re.compile(r"\d+")

#: Обрамление, которым модели любят украшать ответ.
FENCE_PATTERN = re.compile(r"```[a-zA-Z]*")

_QUOTE_PAIRS = (("«", "»"), ('"', '"'), ("'", "'"), ("“", "”"))

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "да"})


# ---------------------------------------------------------------------------
# Переключатель
# ---------------------------------------------------------------------------


def env_value(name: str, env_file: Path | None = None) -> str:
    """Значение переменной из окружения или из `.env` рядом с проектом.

    Тем же способом, которым :func:`faq_bot.speech.api_key` достаёт ключ:
    человек кладёт настройки в один файл и не обязан помнить, какой
    модуль читает окружение, а какой файл.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = ENV_FILE if env_file is None else env_file
    try:
        if not path.exists():
            return ""
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition("=")
            if key.strip() == name:
                return raw.strip()
    except OSError:                                  # pragma: no cover
        return ""
    return ""


def flag(name: str, env_file: Path | None = None) -> bool:
    """Включён ли переключатель. Неустановленная переменная — «нет».

    Значение по умолчанию выбрано в пользу чужого кошелька: модуль,
    про который забыли, не тратит ничего.
    """
    return env_value(name, env_file).lower() in _TRUE_VALUES


#: Выключено по умолчанию. Включается `REPHRASE_ENABLED=1` в окружении
#: или в `.env`. Жюри прогоняет `answer()`, где модуля нет вовсе, поэтому
#: включать его нужно осознанно — на демонстрации, а не «на всякий случай».
ENABLED = flag("REPHRASE_ENABLED")


# ---------------------------------------------------------------------------
# Результат
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rephrased:
    """Что показать абоненту и чем это обосновано.

    Поле `text` безопасно всегда: это либо проверенная переформулировка,
    либо дословный ответ из faq.json. Поле `source` говорит, откуда текст
    взялся и были ли потрачены деньги; `verdict` — вердикт сторожа
    фактов, если модель вообще что-то ответила; `reason` — почему
    переформулировки нет.
    """

    text: str
    source: str
    verdict: consistency.Verdict | None = None
    reason: str = ""
    cached: bool = False

    @property
    def rewritten(self) -> bool:
        """Показана переформулировка, а не цитата из базы."""
        return self.source == LLM

    def explain(self) -> str:
        """Строка для лога и для демонстрации жюри."""
        head = {LLM: "переформулировано",
                FAQ: "отклонено, текст из базы",
                SKIPPED: "модель не вызывалась"}.get(self.source, self.source)
        parts = [head]
        if self.cached:
            parts.append("из кеша")
        if self.reason:
            parts.append(self.reason)
        return ": ".join(parts)


# ---------------------------------------------------------------------------
# Бюджет вызовов
# ---------------------------------------------------------------------------


class CallBudget:
    """Сколько обращений к модели сделано — по чату, за час и за сутки.

    Устроена как :class:`faq_bot.speech.Budget`, но единица учёта —
    вызов, а не секунда звука. Хранится в памяти процесса: перезапуск
    обнуляет, и для хакатона это честнее, чем база ради счётчика.
    """

    def __init__(self) -> None:
        self._events: list[tuple[float, str]] = []

    def _count(self, since: float, chat_id: str | None = None) -> int:
        return sum(1 for moment, chat in self._events
                   if moment >= since and (chat_id is None or chat == chat_id))

    def allows(self, chat_id: str | None) -> tuple[bool, str]:
        """Можно ли обратиться к модели прямо сейчас."""
        now = time.time()
        hour, day = now - 3600, now - 86400

        if chat_id is not None:
            used = self._count(hour, str(chat_id))
            if used >= CALLS_PER_CHAT_HOUR:
                return False, f"чат израсходовал {used} вызовов за час"
        if self._count(hour) >= CALLS_TOTAL_HOUR:
            return False, "общий часовой лимит вызовов исчерпан"
        if self._count(day) >= CALLS_TOTAL_DAY:
            return False, "общий суточный лимит вызовов исчерпан"
        return True, ""

    def record(self, chat_id: str | None) -> None:
        """Списать попытку.

        Списывается ДО обращения к сети. Причина: неудачный вызов мы
        всё равно не повторяем, а упавшее исключение не должно оставить
        счётчик нетронутым — иначе битый endpoint можно дёргать вечно.
        """
        now = time.time()
        self._events = [item for item in self._events if item[0] >= now - 86400]
        self._events.append((now, str(chat_id)))

    def report(self) -> dict[str, int]:
        """Сколько вызовов потрачено — для /stats и для совести."""
        now = time.time()
        return {
            "за час": self._count(now - 3600),
            "за сутки": self._count(now - 86400),
            "лимит в сутки": CALLS_TOTAL_DAY,
        }

    def clear(self) -> None:
        self._events.clear()


BUDGET = CallBudget()

#: Кеш результатов: `(faq_id, язык, нормализованный вопрос)` -> результат.
CACHE: dict[tuple[int, str, str], Rephrased] = {}


def reset() -> None:
    """Забыть кеш и счётчики. Нужна тестам и перезапуску бота."""
    CACHE.clear()
    BUDGET.clear()


def report() -> dict[str, object]:
    """Состояние модуля для `/stats`: включён ли, что потрачено."""
    return {"включено": ENABLED, "кеш, записей": len(CACHE),
            "вызовы": BUDGET.report()}


# ---------------------------------------------------------------------------
# Промпт и транспорт
# ---------------------------------------------------------------------------


def build_prompt(question: str, answer: str, language: str) -> str:
    """Указание модели. Коротко: каждый токен оплачен.

    В промпте нет ни базы целиком, ни истории диалога — только вопрос и
    найденная запись. Это и есть RAG: модель физически не знает других
    фактов, кроме переданного ответа, и не может сослаться на них.
    """
    name = LANGUAGE_NAMES.get(language, LANGUAGE_NAMES["ru"])
    short = question.strip()[:MAX_QUESTION_CHARS]
    return (
        f"Вопрос абонента: {short}\n"
        f"Ответ из базы: {answer}\n\n"
        "Перепиши ответ из базы так, чтобы он отвечал именно на этот "
        "вопрос. Нельзя добавлять факты, числа, коды, цены, сроки и "
        f"условия, которых нет в ответе из базы. Язык ответа — {name}. "
        "Верни только текст ответа, без пояснений."
    )


def transport(url: str, body: dict, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Единственное место, где модуль выходит в сеть.

    Вынесено отдельной функцией ради тестов: они подменяют её целиком и
    поэтому не могут случайно обратиться к настоящему API.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_body(prompt: str) -> dict:
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "candidateCount": 1,
        },
    }


def _truncated(payload: object) -> bool:
    """Оборвался ли ответ на потолке токенов.

    Обрезанная на полуслове фраза хуже дословной цитаты, поэтому такой
    ответ не показываем.
    """
    try:
        return payload["candidates"][0].get("finishReason") == "MAX_TOKENS"
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Сторожа
# ---------------------------------------------------------------------------


def clean(raw: str) -> str:
    """Убрать украшения модели: переводы строк, кавычки, ``` блоки."""
    text = " ".join(str(raw).split())
    text = FENCE_PATTERN.sub(" ", text)
    text = " ".join(text.split()).strip()
    for left, right in _QUOTE_PAIRS:
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
    return text


def _entry_source(entry: FaqEntry) -> str:
    """Весь текст записи, который считается правдой.

    Берутся оба языка: кыргызская и русская версии описывают одну услугу,
    и числа в них одни и те же.
    """
    return " ".join((entry.question_ru, entry.question_ky,
                     entry.answer_ru, entry.answer_ky))


def invented_numbers(text: str, entry: FaqEntry) -> tuple[str, ...]:
    """Группы цифр, которых в записи базы нет.

    Дополняет сторожа связей: тот проверяет USSD-коды, цены и объёмы, но
    часы работы «09:00–18:00» и проценты комиссии под его шаблоны не
    попадают. Сравнение дословное, без «похожести»: «8:00» вместо «18:00»
    обязано считаться выдумкой.
    """
    allowed = set(DIGITS_PATTERN.findall(_entry_source(entry)))
    found = DIGITS_PATTERN.findall(text)
    return tuple(dict.fromkeys(item for item in found if item not in allowed))


def _rejection(text: str, entry: FaqEntry, language: str,
               base: str) -> tuple[str, consistency.Verdict] | None:
    """Почему переформулировку нельзя показывать. None — можно.

    Порядок: сначала главный сторож фактов (это суть бонуса), потом
    дешёвые дополнительные правила.
    """
    verdict = consistency.check(text, entry)
    if not verdict.consistent:
        return f"сторож фактов: {verdict.reason}", verdict

    invented = invented_numbers(text, entry)
    if invented:
        return f"выдуманные числа: {', '.join(invented)}", verdict

    if len(text) > MAX_LENGTH_RATIO * len(base):
        return (f"длиннее исходного в {len(text) / len(base):.1f} раза",
                verdict)

    if len(text) < MIN_TEXT_CHARS:
        return f"слишком короткий ответ: {len(text)} знаков", verdict

    if detect(text) != language:
        return f"язык ответа не {language}", verdict

    return None


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------


def rephrase(question: str, entry: FaqEntry, language: str, *,
             chat_id: str | None = None,
             model: str = DEFAULT_MODEL) -> Rephrased:
    """Ответ темы, переписанный под вопрос. Никогда не бросает исключение.

    При любой неудаче — выключено, нет ключа, нет сети, таймаут, мусор
    вместо JSON, сработавший сторож — возвращается дословный
    `entry.answer(language)`. Вызывающему коду не нужно ничего
    обрабатывать: поле `text` можно показывать всегда.
    """
    base = _base_answer(entry, language)
    try:
        return _rephrase(question, entry, language, base, chat_id, model)
    except Exception as error:                      # последний рубеж
        logger.warning("Переформулировка не удалась: %s", error)
        return Rephrased(base, SKIPPED, reason=f"внутренний сбой: {error}")


def _base_answer(entry: FaqEntry, language: str) -> str:
    """Дословный ответ темы. Пустая строка, если записи фактически нет."""
    try:
        return entry.answer(language) or ""
    except Exception:                               # pragma: no cover
        return ""


def _rephrase(question: str, entry: FaqEntry, language: str, base: str,
              chat_id: str | None, model: str) -> Rephrased:
    """Тело :func:`rephrase`. Проверки идут от бесплатных к платным."""
    if not ENABLED:
        return Rephrased(base, SKIPPED,
                         reason="выключено (REPHRASE_ENABLED)")

    normalized = normalize(question)
    if not normalized or not base.strip():
        return Rephrased(base, SKIPPED, reason="нечего переформулировать")

    key = (int(entry.id), language, normalized)
    remembered = CACHE.get(key)
    if remembered is not None:
        return replace(remembered, cached=True)

    api = api_key()
    if not api:
        logger.info("Нет GEMINI_API_KEY — переформулировка пропущена")
        return Rephrased(base, SKIPPED, reason="нет ключа GEMINI_API_KEY")

    allowed, limit_reason = BUDGET.allows(chat_id)
    if not allowed:
        logger.warning("Переформулировка отклонена для чата %s: %s",
                       chat_id, limit_reason)
        return Rephrased(base, SKIPPED, reason=limit_reason)

    url = ENDPOINT.format(model=model) + f"?key={api}"
    body = _request_body(build_prompt(question, base, language))

    # Списываем попытку до обращения к сети: см. CallBudget.record.
    BUDGET.record(chat_id)
    try:
        payload = transport(url, body)
    except Exception as error:                      # сеть, таймаут, разбор
        logger.warning("Модель не ответила: %s", error)
        return Rephrased(base, FAQ, reason=f"модель недоступна: {error}")

    if _truncated(payload):
        return _remember(key, Rephrased(base, FAQ,
                                        reason="ответ оборван на потолке"))

    text = clean(extract_text(payload) if isinstance(payload, dict) else "")
    if not text:
        return _remember(key, Rephrased(base, FAQ,
                                        reason="модель вернула пустой текст"))

    if normalize(text) == normalize(base):
        # Модель ничего не изменила — показываем базу, чтобы не тащить
        # случайные расхождения в пунктуации.
        return _remember(key, Rephrased(base, FAQ,
                                        reason="модель вернула исходный текст",
                                        verdict=consistency.check(text, entry)))

    rejection = _rejection(text, entry, language, base)
    if rejection is not None:
        reason, verdict = rejection
        logger.warning("Переформулировка темы %s отклонена — %s",
                       entry.id, reason)
        return _remember(key, Rephrased(base, FAQ, verdict=verdict,
                                        reason=reason))

    logger.info("Тема %s переформулирована: %d знаков вместо %d",
                entry.id, len(text), len(base))
    return _remember(key, Rephrased(text, LLM,
                                    verdict=consistency.check(text, entry)))


def _remember(key: tuple[int, str, str], result: Rephrased) -> Rephrased:
    """Запомнить исход вызова модели.

    Кешируются и отказы: тот же вопрос по той же теме даст то же
    нарушение, и платить за него второй раз незачем. Не кешируются
    только сбои связи — сеть лежит временно, а кеш навсегда.
    """
    if result.reason.startswith("модель недоступна"):
        return result
    if len(CACHE) >= CACHE_LIMIT:
        CACHE.pop(next(iter(CACHE)), None)
    CACHE[key] = result
    return result
