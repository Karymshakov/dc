"""Проверки переформулировки ответа моделью.

Главное правило этого файла: **настоящий API не вызывается ни разу**.
Транспорт подменяется автоматической фикстурой, и по умолчанию он падает
с ошибкой — тест, который забыл подставить свою заглушку, упадёт на
попытке выйти в сеть, а не потратит чужие деньги.

Тесты записаны от требования, а не от реализации: проверяется, что
абонент получает дословный текст из faq.json при любом сбое, и что
переформулировка проходит только тогда, когда она ничего не выдумала.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from faq_bot import config, consistency
from faq_bot import rephrase as module
from faq_bot.contracts import FaqEntry
from faq_bot.lang import detect

# --- Оснастка ---------------------------------------------------------------


@pytest.fixture(scope="module")
def by_id() -> dict[int, FaqEntry]:
    """Записи прямо из faq.json: проверяем на настоящих данных.

    Файл читается напрямую, без `knowledge.load_faq`: тест про
    переформулировку не должен падать из-за слоя синонимов.
    """
    raw = json.loads(config.FAQ_PATH.read_text(encoding="utf-8"))
    return {item["id"]: FaqEntry(**item) for item in raw}


def _forbidden(*args, **kwargs):
    """Транспорт по умолчанию: обращение к сети — ошибка теста."""
    raise AssertionError("тест попытался обратиться к настоящему API")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Ни сети, ни ключа из `.env`, ни состояния предыдущего теста.

    Модуль включается принудительно: выключенное состояние проверяется
    отдельным тестом, а все остальные проверяют поведение включённого
    модуля и не должны зависеть от содержимого `.env` разработчика.
    """
    module.reset()
    monkeypatch.setattr(module, "transport", _forbidden)
    monkeypatch.setattr(module, "api_key", lambda: "ключ-для-теста")
    monkeypatch.setattr(module, "ENABLED", True)
    yield
    module.reset()


class Raw:
    """«Вернуть это как есть»: так в тест попадает мусор вместо ответа.

    Без обёртки строка считалась бы текстом ответа модели, и проверка
    мусора проверяла бы совсем другое.
    """

    def __init__(self, payload: object) -> None:
        self.payload = payload


class FakeModel:
    """Заглушка транспорта: отдаёт заданные ответы и считает вызовы.

    Строка превращается в обычный ответ Gemini, :class:`Raw` отдаётся
    без обёртки, исключение бросается.
    """

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies) or [""]
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.bodies)

    def __call__(self, url: str, body: dict, timeout: float = 0.0) -> dict:
        self.bodies.append(body)
        self.urls.append(url)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Raw):
            return reply.payload              # type: ignore[return-value]
        return {"candidates": [{"content": {"parts": [{"text": reply}]}}]}


def install(monkeypatch, *replies: object) -> FakeModel:
    fake = FakeModel(*replies)
    monkeypatch.setattr(module, "transport", fake)
    return fake


def prompt_of(fake: FakeModel, index: int = 0) -> str:
    return fake.bodies[index]["contents"][0]["parts"][0]["text"]


#: Удачная переформулировка темы 14 под вопрос «оператор»: инструкция
#: «напишите "оператор"» заменена на ответ тому, кто её уже выполнил.
GOOD_RU = "Поддержка уже здесь: напишите вопрос, отвечаем ежедневно с 09:00 до 18:00."
GOOD_KY = "Колдоо ушул жерде: сурооңузду жазыңыз, күн сайын 09:00–18:00 жооп беребиз."


# --- Выключено по умолчанию ------------------------------------------------


def test_module_ships_disabled(monkeypatch, tmp_path):
    """Без переменной окружения модуль выключен.

    Значение по умолчанию выбрано в пользу чужого кошелька: на
    автопроверке жюри идёт `answer()`, где модели нет вовсе.
    """
    monkeypatch.delenv("REPHRASE_ENABLED", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("TELEGRAM_BOT_TOKEN=123\n", encoding="utf-8")

    assert module.flag("REPHRASE_ENABLED", empty) is False


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("on", True), ("да", True),
    ("0", False), ("false", False), ("no", False), ("", False),
])
def test_flag_reads_the_switch(monkeypatch, value, expected):
    monkeypatch.setenv("REPHRASE_ENABLED", value)

    assert module.flag("REPHRASE_ENABLED") is expected


def test_disabled_makes_no_calls_at_all(monkeypatch, by_id):
    """Выключенный модуль не тратит ни одного токена."""
    monkeypatch.setattr(module, "ENABLED", False)
    fake = install(monkeypatch, GOOD_RU)
    entry = by_id[14]

    result = module.rephrase("оператор", entry, "ru")

    assert fake.calls == 0
    assert result.text == entry.answer_ru
    assert result.source == module.SKIPPED
    assert "REPHRASE_ENABLED" in result.reason


# --- Главное: без выдумки фактов -------------------------------------------


def test_foreign_ussd_code_is_rejected(monkeypatch, by_id):
    """Ключевой случай D-09: код настоящий, тема чужая.

    `*111#` существует и сработает, но принадлежит доп. интернету.
    В ответе про баланс он означает, что абонент наберёт рабочую команду
    и получит не то, за чем пришёл.
    """
    entry = by_id[1]
    install(monkeypatch, "Наберите *111#. Проверка бесплатная.")

    result = module.rephrase("как узнать баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ
    assert result.verdict is not None and result.verdict.consistent is False
    assert "*111#" in result.verdict.explain()


def test_price_built_from_a_code_is_still_a_foreign_fact(monkeypatch, by_id):
    """Число из темы, превращённое в другое утверждение, не проходит.

    В теме 1 есть «100» — но как часть кода `*100#`, а не как цена.
    Проверка «есть ли такое значение в записи» это пропустила бы;
    проверка связи видит, что цены у темы нет вовсе.
    """
    entry = by_id[1]
    install(monkeypatch, "Наберите *100#. Проверка стоит 100 сом.")

    result = module.rephrase("сколько стоит проверка баланса", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ
    assert result.verdict is not None and result.verdict.consistent is False


def test_invented_working_hours_are_rejected(monkeypatch, by_id):
    """Новых цифр не бывает — даже там, где сторож связей их не видит.

    Часы работы под шаблоны кодов и цен не попадают, поэтому действует
    второе правило: любая группа цифр обязана быть в записи базы.
    """
    entry = by_id[14]
    install(monkeypatch, "Поддержка отвечает ежедневно с 09:00 до 20:00.")

    result = module.rephrase("когда работает поддержка", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ
    assert "20" in result.reason


def test_rambling_answer_is_rejected(monkeypatch, by_id):
    """Разговорившаяся модель почти наверняка присочинила.

    Ответ справочной короток по определению; удвоение длины — признак
    добавленного содержания, даже если проверяемых фактов в нём нет.
    """
    entry = by_id[1]
    install(monkeypatch,
            "Проверить остаток средств можно бесплатно и в любое время, " * 3)

    result = module.rephrase("баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ
    assert "длиннее" in result.reason


def test_truncated_answer_is_rejected(monkeypatch, by_id):
    """Фраза, оборванная на потолке токенов, хуже дословной цитаты."""
    entry = by_id[14]
    install(monkeypatch, Raw({"candidates": [{
        "finishReason": "MAX_TOKENS",
        "content": {"parts": [{"text": "Поддержка отвечает ежедневно с"}]},
    }]}))

    result = module.rephrase("оператор", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ


def test_good_rephrase_passes(monkeypatch, by_id):
    """Живой случай, ради которого всё написано.

    Абонент пишет «оператор» — то есть инструкцию «напишите "оператор"»
    он уже выполнил. Переформулировка отвечает ему, а не повторяет
    инструкцию, и при этом не добавляет ни одного факта.
    """
    entry = by_id[14]
    fake = install(monkeypatch, GOOD_RU)

    result = module.rephrase("оператор", entry, "ru")

    assert result.source == module.LLM
    assert result.text == GOOD_RU
    assert result.rewritten is True
    assert result.verdict is not None and result.verdict.consistent is True
    assert fake.calls == 1


def test_model_decorations_are_stripped(monkeypatch, by_id):
    """Кавычки и ``` вокруг ответа — оформление, а не текст ответа."""
    entry = by_id[14]
    install(monkeypatch, f'```\n"{GOOD_RU}"\n```')

    result = module.rephrase("оператор", entry, "ru")

    assert result.source == module.LLM
    assert result.text == GOOD_RU


def test_repeated_source_text_returns_the_source(monkeypatch, by_id):
    """Если модель ничего не изменила, показываем базу дословно."""
    entry = by_id[1]
    install(monkeypatch, entry.answer_ru)

    result = module.rephrase("баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.FAQ


# --- Язык ------------------------------------------------------------------


def test_kyrgyz_answer_stays_kyrgyz(monkeypatch, by_id):
    """ТЗ требует отвечать на языке вопроса — проверяем это кодом."""
    entry = by_id[14]
    install(monkeypatch, GOOD_KY)

    result = module.rephrase("оператор менен байланышуу", entry, "ky",
                             chat_id="77")

    assert result.source == module.LLM
    assert result.text == GOOD_KY
    assert detect(result.text) == "ky"


def test_russian_rephrase_of_a_kyrgyz_answer_is_rejected(monkeypatch, by_id):
    """Съехавший язык — тоже брак, хотя факты на месте."""
    entry = by_id[14]
    install(monkeypatch,
            "Поддержка работает ежедневно с 09:00 до 18:00, напишите вопрос.")

    result = module.rephrase("оператор керек", entry, "ky")

    assert result.text == entry.answer_ky
    assert result.source == module.FAQ
    assert "язык" in result.reason


def test_language_chooses_the_source_text(monkeypatch, by_id):
    """Модели даётся ответ на том языке, на котором её просят писать."""
    entry = by_id[14]
    fake = install(monkeypatch, GOOD_KY)

    module.rephrase("оператор керек", entry, "ky")

    assert entry.answer_ky in prompt_of(fake)
    assert "кыргызский" in prompt_of(fake)


# --- Ничего не падает ------------------------------------------------------


def test_missing_key_skips_without_touching_the_network(monkeypatch, by_id):
    """Нет ключа — нет вызова. Транспорт остаётся запрещающим."""
    monkeypatch.setattr(module, "api_key", lambda: "")
    entry = by_id[1]

    result = module.rephrase("как узнать баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source == module.SKIPPED
    assert "ключ" in result.reason


@pytest.mark.parametrize("failure", [
    TimeoutError("время вышло"),
    urllib.error.URLError("сеть недоступна"),
    urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None),
    json.JSONDecodeError("битый JSON", "{", 0),
    RuntimeError("что-то совсем неожиданное"),
])
def test_every_failure_gives_the_source_text(monkeypatch, by_id, failure):
    """Необязательное улучшение не имеет права уронить ответ."""
    entry = by_id[1]
    install(monkeypatch, failure)

    result = module.rephrase("как узнать баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source in (module.FAQ, module.SKIPPED)


@pytest.mark.parametrize("payload", [
    {}, None, [], "не словарь вовсе", 42,
    {"candidates": []},
    {"candidates": "мусор"},
    {"candidates": [{"content": {"parts": []}}]},
    {"candidates": [{"content": {"parts": [{"text": "   "}]}}]},
    {"candidates": [{"content": {"parts": "не список"}}]},
])
def test_garbage_payload_gives_the_source_text(monkeypatch, by_id, payload):
    """Мусор вместо ответа модели разбирается без исключений."""
    entry = by_id[1]
    install(monkeypatch, Raw(payload))

    result = module.rephrase("как узнать баланс", entry, "ru")

    assert result.text == entry.answer_ru
    assert result.source in (module.FAQ, module.SKIPPED)


def test_empty_question_costs_nothing(monkeypatch, by_id):
    """Пустой вопрос переформулировать нечем, и платить за это не нужно."""
    fake = install(monkeypatch, GOOD_RU)
    entry = by_id[14]

    result = module.rephrase("   ", entry, "ru")

    assert fake.calls == 0
    assert result.text == entry.answer_ru
    assert result.source == module.SKIPPED


def test_entry_without_an_answer_is_survived(monkeypatch):
    """Пустая запись не ломает вызов — возвращается пустой текст."""
    install(monkeypatch, GOOD_RU)
    broken = FaqEntry(id=99, question_ru="", question_ky="",
                      answer_ru="", answer_ky="")

    result = module.rephrase("оператор", broken, "ru")

    assert result.text == ""
    assert result.source == module.SKIPPED


# --- Экономия --------------------------------------------------------------


def test_repeated_question_is_free(monkeypatch, by_id):
    """Второй такой же вопрос не стоит ничего."""
    entry = by_id[14]
    fake = install(monkeypatch, GOOD_RU)

    first = module.rephrase("оператор", entry, "ru", chat_id="1")
    second = module.rephrase("Оператор!", entry, "ru", chat_id="2")

    assert fake.calls == 1, "нормализованный вопрос тот же — вызов один"
    assert second.text == first.text
    assert second.cached is True and first.cached is False
    assert second.source == module.LLM


def test_rejected_answer_is_also_remembered(monkeypatch, by_id):
    """Отклонённый ответ кешируется: нарушение повторится, плата нет."""
    entry = by_id[1]
    fake = install(monkeypatch, "Наберите *111#. Проверка бесплатная.")

    module.rephrase("как узнать баланс", entry, "ru")
    again = module.rephrase("как узнать баланс", entry, "ru")

    assert fake.calls == 1
    assert again.text == entry.answer_ru
    assert again.cached is True


def test_cache_separates_languages_and_topics(monkeypatch, by_id):
    """Ключ кеша — тема, язык и вопрос: иначе ответ уедет не туда."""
    fake = install(monkeypatch, GOOD_RU, GOOD_KY, GOOD_RU)

    module.rephrase("оператор", by_id[14], "ru")
    module.rephrase("оператор", by_id[14], "ky")
    module.rephrase("оператор", by_id[13], "ru")

    assert fake.calls == 3


def test_network_failure_is_not_cached(monkeypatch, by_id):
    """Сеть лежит временно, а кеш навсегда — сбой связи не запоминаем."""
    entry = by_id[14]
    fake = install(monkeypatch, TimeoutError("время вышло"), GOOD_RU)

    first = module.rephrase("оператор", entry, "ru")
    second = module.rephrase("оператор", entry, "ru")

    assert fake.calls == 2
    assert first.text == entry.answer_ru
    assert second.source == module.LLM


def test_exhausted_chat_budget_stops_before_the_network(monkeypatch, by_id):
    """Упёршийся в лимит чат не доходит до сети: транспорт запрещающий."""
    entry = by_id[14]
    for _ in range(module.CALLS_PER_CHAT_HOUR):
        module.BUDGET.record("555")

    result = module.rephrase("оператор", entry, "ru", chat_id="555")

    assert result.text == entry.answer_ru
    assert result.source == module.SKIPPED
    assert "лимит" in result.reason or "израсходовал" in result.reason


def test_exhausted_daily_budget_stops_every_chat(monkeypatch, by_id):
    """Суточный предел общий: одним чатом его не обойти."""
    entry = by_id[14]
    for number in range(module.CALLS_TOTAL_DAY):
        module.BUDGET.record(f"чат-{number}")

    result = module.rephrase("оператор", entry, "ru", chat_id="новый")

    assert result.source == module.SKIPPED
    assert result.text == entry.answer_ru


def test_attempt_is_charged_even_when_the_call_fails(monkeypatch, by_id):
    """Неудачный вызов списывается: битый endpoint нельзя дёргать вечно."""
    install(monkeypatch, TimeoutError("время вышло"))

    module.rephrase("оператор", by_id[14], "ru", chat_id="9")

    assert module.BUDGET.report()["за час"] == 1


def test_budget_counts_per_chat(monkeypatch, by_id):
    """Чужой чат не расходует лимит соседа."""
    for _ in range(module.CALLS_PER_CHAT_HOUR):
        module.BUDGET.record("шумный")

    allowed, _ = module.BUDGET.allows("тихий")

    assert allowed is True


def test_request_is_cheap_by_construction(monkeypatch, by_id):
    """Промпт короткий, потолок вывода жёсткий, модель самая дешёвая."""
    entry = by_id[1]
    fake = install(monkeypatch, "Проверка баланса бесплатна, наберите *100#.")

    module.rephrase("скока денег на счету", entry, "ru")

    body = fake.bodies[0]
    assert body["generationConfig"]["maxOutputTokens"] == module.MAX_OUTPUT_TOKENS
    assert body["generationConfig"]["maxOutputTokens"] <= 256
    assert body["generationConfig"]["temperature"] <= 0.3
    assert "flash-lite" in fake.urls[0]
    assert module.DEFAULT_MODEL in fake.urls[0]


def test_prompt_carries_only_the_found_entry(monkeypatch, by_id):
    """Это RAG: модель не знает фактов, кроме переданной записи.

    Чужих кодов в промпте нет физически, поэтому сослаться на них модель
    может только выдумав — а выдумку поймает сторож.
    """
    entry = by_id[1]
    fake = install(monkeypatch, "Проверка баланса бесплатна, наберите *100#.")

    module.rephrase("скока денег на счету", entry, "ru")

    prompt = prompt_of(fake)
    assert "скока денег на счету" in prompt
    assert entry.answer_ru in prompt
    assert "*111#" not in prompt
    assert by_id[7].answer_ru not in prompt


def test_long_question_is_trimmed(monkeypatch, by_id):
    """За токены вопроса платим мы, поэтому вопрос обрезается."""
    fake = install(monkeypatch, GOOD_RU)

    module.rephrase("оператор " * 200, by_id[14], "ru")

    assert len(prompt_of(fake)) < 1000


# --- Самопроверка модуля ---------------------------------------------------


def test_source_values_are_the_agreed_three():
    assert (module.LLM, module.FAQ, module.SKIPPED) == ("llm", "faq", "skipped")


def test_guard_is_the_project_consistency_check(by_id):
    """Сторож тот же, что у бота: отдельной копии правил нет.

    Если проверка согласованности изменится, переформулировка изменится
    вместе с ней — это одна защита, а не две похожие.
    """
    entry = by_id[1]
    rejection = module._rejection("Наберите *111#.", entry, "ru",
                                  entry.answer_ru)

    assert rejection is not None
    assert rejection[1] == consistency.check("Наберите *111#.", entry)


def test_report_shows_what_was_spent(monkeypatch, by_id):
    install(monkeypatch, GOOD_RU)

    module.rephrase("оператор", by_id[14], "ru", chat_id="1")
    state = module.report()

    assert state["кеш, записей"] == 1
    assert state["вызовы"]["за сутки"] == 1
