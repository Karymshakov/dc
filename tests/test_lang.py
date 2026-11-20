"""Тесты определения языка: ru или ky, третьего значения нет.

Здесь проверяется правило из ТЗ и уточнений организаторов, а не
устройство детектора: внутренние функции, веса и словари намеренно не
импортируются. Если решение перепишут целиком другим способом, эти
тесты обязаны продолжать работать.

Фразы придуманы для тестов и не копируются из размеченных наборов,
кроме одного итогового замера по открытому `tune.csv`.
"""
from __future__ import annotations

import csv

import pytest

from faq_bot import config
from faq_bot.contracts import LanguageDetector
from faq_bot.lang import DETECTOR, RuleLanguageDetector, detect

# --- контракт ---------------------------------------------------------------


def test_детектор_удовлетворяет_протоколу() -> None:
    assert isinstance(DETECTOR, LanguageDetector)
    assert isinstance(RuleLanguageDetector(), LanguageDetector)


@pytest.mark.parametrize(
    "text",
    [
        "Как проверить баланс?",
        "Балансты кантип текшерем?",
        "Balansymdy kantip korom",
        "",
        "   ",
        "12345",
        "😀",
        "\x00\x01",
        "a" * 500,
        "Как проверить баланс" * 50,
    ],
)
def test_ответ_всегда_один_из_двух_языков(text: str) -> None:
    """Исключений нет, третьего значения нет — что бы ни пришло на вход."""
    assert detect(text) in config.LANGUAGES


# --- 1. Буквы ң, ө, ү -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Тарифимди өзгөрткүм келет",
        "Өзүмдүн номерим кайсы?",
        "Бөгөттөө",
        "Көрсөтүү",
        "Сүйлөшүү",
        "Жаңыртуу",
    ],
    ids=["ө-и-ү", "өзүмдүн", "только-ө", "только-ө-2", "только-ү", "только-ң"],
)
def test_буквы_ng_oe_ue_дают_кыргызский(text: str) -> None:
    """ң, ө, ү вне кыргызского не встречаются — этого признака достаточно."""
    assert detect(text) == "ky"


# --- 2. Кыргызский без этих букв --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "SIM жоголду, кайра алсам болобу?",
        "Балансты кантип текшерем",
        "Акча которсом болобу",
        "Интернет иштебей жатат",
        "Эсепке акча салсам болобу",
        "Канча акча калды",
        "Жардам керек",
        "Тарифти алмаштыргым келет",
    ],
)
def test_кыргызский_без_особых_букв_по_окончаниям_и_словарю(
    text: str,
) -> None:
    assert detect(text) == "ky"


# --- 3. Кыргызская латиница -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Balansymdy kantip korom",
        "Internet ishtebei jatat",
        "Internet ishtebey zhatat",
        "Tarifti almashtyrgim kelet",
        "Kantıp balansymdy korom",
        "Esepke akcha salsam bolobu",
        "Jardam kerek",
        "Nomerimdi bilgim kelet",
    ],
)
def test_кыргызская_латиница(text: str) -> None:
    assert detect(text) == "ky"


def test_разнобой_написания_не_меняет_язык() -> None:
    """Три написания одной фразы обязаны дать один и тот же ответ."""
    variants = [
        "Internet ishtebei jatat",
        "Internet ishtebey jatat",
        "Internet ishtebey zhatat",
    ]
    assert {detect(text) for text in variants} == {"ky"}


# --- 4. Русская латиница ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Kak proverit balans",
        "Skolko stoit roaming",
        "Pochemu ne rabotaet internet",
        "Kak popolnit schet",
        "Kak uznat svoy nomer",
        "Pomogite zablokirovat sim kartu",
        "Hochu smenit tarif",
        "Gde posmotret ostatok minut",
    ],
)
def test_русская_латиница_остаётся_русской(text: str) -> None:
    """Латиница сама по себе не признак кыргызского."""
    assert detect(text) == "ru"


# --- 5. Смешанная речь: решает грамматическая основа ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Роуминг канча турат?",
        "Детализация алгым келет",
        "Подпискаларды өчүргүм келет",
        "SIM картаны блокировать керек",
        "Тариф боюнча маалымат керек",
    ],
)
def test_русский_термин_не_меняет_кыргызскую_основу(text: str) -> None:
    assert detect(text) == "ky"


@pytest.mark.parametrize(
    "text",
    [
        "Как подключить роуминг?",
        "Хочу узнать баланс, рахмат",
        "Мне нужен интернет жана смс пакет",
        "Сколько стоит гигабайт интернета",
    ],
)
def test_кыргызское_слово_в_русской_основе_не_меняет_язык(text: str) -> None:
    assert detect(text) == "ru"


def test_пара_одинаковых_по_теме_фраз_разводится_по_языкам() -> None:
    assert detect("Как включить роуминг и сколько он стоит?") == "ru"
    assert detect("Роумингди кантип кошом жана баасы канча?") == "ky"


# --- 6-7. Пограничные входы: значение по умолчанию --------------------------


@pytest.mark.parametrize(
    "text",
    ["", " ", "    ", "\t", "\n", " "],
    ids=["пусто", "пробел", "пробелы", "таб", "перевод-строки", "nbsp"],
)
def test_пустой_вопрос_русский(text: str) -> None:
    """Уточнения организаторов: «Пустой вопрос: ru, faq_id=null»."""
    assert detect(text) == config.DEFAULT_LANGUAGE == "ru"


@pytest.mark.parametrize(
    "text",
    ["12345", "0", "100 500", "😀😀😀", "???", "*100#", "...", "+996"],
    ids=["цифры", "ноль", "числа", "эмодзи", "знаки", "ussd", "точки",
         "номер"],
)
def test_текст_без_языковых_признаков_русский_по_умолчанию(text: str) -> None:
    """Это именно значение по умолчанию, а не особый случай."""
    assert detect(text) == "ru"


# --- Русский, который похож на кыргызский (и наоборот) ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "Прогноз погоды на завтра",
        "Самым дешёвым тарифом пользуюсь",
        "Телефоном не могу позвонить",
        "Страны роуминга",
        "Старым клиентам скидки есть",
        "Остаток гигабайт покажите",
        "Подписки отключите",
        "Он берёт деньги за перевод",
    ],
    ids=[
        "погоды-не-кыргызское-ды", "самым-не-кыргызское-ым",
        "телефоном-не-кыргызское-ом", "страны-не-кыргызское-ны",
        "старым-и-клиентам", "гигабайт-не-глагол",
        "подписки", "берёт-не-кыргызское-ет",
    ],
)
def test_русские_слова_с_похожими_окончаниями_не_становятся_кыргызскими(
    text: str,
) -> None:
    """Сингармонизм: после «о» кыргызский требует «-ду», а не «-ды»."""
    assert detect(text) == "ru"


@pytest.mark.parametrize(
    "text",
    [
        "Эсебимди текшерип бер",
        "Кабинеттеги маалымат жок",
        "Тарифимдин баасы канча",
        "Акысыз текшерүү барбы",
        "Канчадан турат",
    ],
)
def test_кыргызская_морфология_узнаётся_на_разных_окончаниях(
    text: str,
) -> None:
    assert detect(text) == "ky"


# --- Устойчивость -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "как проверить баланс",
        "КАК ПРОВЕРИТЬ БАЛАНС",
        "Как проверить баланс?!",
        "  Как  проверить  баланс  ",
    ],
)
def test_регистр_и_пунктуация_не_меняют_язык(text: str) -> None:
    assert detect(text) == "ru"


def test_функция_и_класс_дают_одинаковый_ответ() -> None:
    detector = RuleLanguageDetector()
    for text in ("Канча акча калды", "Сколько денег на счету", ""):
        assert detector.detect(text) == detect(text)


# --- Итоговый замер по открытому набору -------------------------------------

TUNE_MIN_ACCURACY = 0.96  # порог жюри 0.90, запас обязателен


@pytest.mark.skipif(
    not config.TUNE_PATH.exists(), reason="tune.csv отсутствует"
)
def test_точность_языка_на_открытом_наборе_с_запасом() -> None:
    """Защита от регресса: holdout здесь не используется намеренно."""
    with config.TUNE_PATH.open(encoding="utf-8-sig", newline="") as handle:
        cases = [(row["text"], row["lang"]) for row in csv.DictReader(handle)]
    assert cases, "открытый набор пуст"
    hits = sum(1 for text, language in cases if detect(text) == language)
    accuracy = hits / len(cases)
    assert accuracy >= TUNE_MIN_ACCURACY, (
        f"язык определён верно в {hits} из {len(cases)} случаев "
        f"({accuracy:.1%}), порог теста {TUNE_MIN_ACCURACY:.0%}"
    )
