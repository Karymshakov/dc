"""Тесты нормализации текста.

Проверяется договор модуля, а не его внутреннее устройство: что именно
обязана сделать `normalize`, что обязана сохранить и чем отличается
«сглаженная» форма `fold`. Поэтому все ожидания записаны строками,
а не вычислены теми же функциями, которые проверяются.
"""
from __future__ import annotations

import pytest

from faq_bot.normalize import fold, normalize, tokens, translit_to_cyrillic

# --- normalize: что обязано произойти ---------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Как проверить БАЛАНС?", "как проверить баланс"),
        ("Скока   денег  на счету?!", "скока денег на счету"),
        ("  Инет не грузит, что делать  ", "инет не грузит что делать"),
        ("Ёлка, ёж", "елка еж"),
        ("Как\tузнать\nномер", "как узнать номер"),
        ("«Подписки» — отключите!", "подписки отключите"),
        ("*100#", "100"),
        ("Баланс 👍 проверить", "баланс проверить"),
    ],
    ids=[
        "регистр", "пунктуация", "края", "ё-в-е",
        "табы-и-переводы", "кавычки-и-тире", "ussd", "эмодзи",
    ],
)
def test_normalize_приводит_текст_к_договорному_виду(
    raw: str, expected: str
) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "!!!", "😀😀", "— — —"])
def test_normalize_пустой_результат_на_тексте_без_букв_и_цифр(
    raw: str,
) -> None:
    assert normalize(raw) == ""


def test_normalize_сохраняет_буквы_ng_oe_ue() -> None:
    """ң, ө, ү — единственный надёжный признак языка, их терять нельзя."""
    assert normalize("Тарифимди ӨЗГӨРТКҮМ келет, жаңы") == (
        "тарифимди өзгөрткүм келет жаңы"
    )


def test_normalize_идемпотентна() -> None:
    once = normalize("  Эмне ҮЧҮН интернет, иштебейт?? ")
    assert normalize(once) == once


def test_normalize_оставляет_цифры() -> None:
    assert normalize("Тариф 4G за 300 сом") == "тариф 4g за 300 сом"


# --- tokens -----------------------------------------------------------------


def test_tokens_это_слова_нормализованного_текста() -> None:
    assert tokens("Сколько, стоит?  роуминг") == [
        "сколько", "стоит", "роуминг",
    ]


@pytest.mark.parametrize("raw", ["", "   ", "???"])
def test_tokens_пустой_список_на_пустом_тексте(raw: str) -> None:
    assert tokens(raw) == []


# --- fold: сглаженная форма для нечёткого поиска ----------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Өзгөртүү", "озгортуу"),
        ("мүнөт", "мунот"),
        ("жаңы", "жаны"),
        ("Балансымды КАНТИП көрөм?", "балансымды кантип кором"),
        ("проверить", "проверит"),
        ("подъезд", "подезд"),
    ],
    ids=["ө-в-о", "ү-в-у", "ң-в-н", "фраза", "мягкий-знак", "твёрдый-знак"],
)
def test_fold_сглаживает_кыргызские_буквы_и_мягкий_знак(
    raw: str, expected: str
) -> None:
    assert fold(raw) == expected


def test_fold_сводит_латиницу_и_кириллицу_к_одной_форме() -> None:
    """Для чего `fold` и нужен поиску: «korom» и «көрөм» обязаны совпасть."""
    assert fold(translit_to_cyrillic("korom")) == fold("көрөм")
    assert fold(translit_to_cyrillic("kantip")) == fold("кантип")


def test_fold_идемпотентна() -> None:
    once = fold("Мүнөттөрүм калдыбы?")
    assert fold(once) == once


# --- translit_to_cyrillic ---------------------------------------------------


@pytest.mark.parametrize(
    "latin, expected",
    [
        ("kantip", "кантип"),
        ("kantıp", "кантып"),
        ("balansymdy", "балансымды"),
        ("kancha", "канча"),
        ("kanday", "кандай"),
        ("kerek", "керек"),
        ("bolobu", "болобу"),
        ("kelet", "келет"),
        ("köröm", "көрөм"),
        ("Balansymdy kantip korom", "балансымды кантип кором"),
        ("Tarifti almashtyrgim kelet", "тарифти алмаштыргим келет"),
    ],
    ids=[
        "kantip", "kantıp-турецкая-ı", "balansymdy", "kancha", "kanday",
        "kerek", "bolobu", "kelet", "köröm", "фраза-1", "фраза-2",
    ],
)
def test_translit_кыргызская_латиница(latin: str, expected: str) -> None:
    assert translit_to_cyrillic(latin) == expected


@pytest.mark.parametrize(
    "first, second",
    [
        ("jatat", "zhatat"),
        ("ishtebei", "ishtebey"),
        ("kantip", "kantıp".replace("ı", "i")),
    ],
    ids=["ж-двумя-способами", "й-двумя-способами", "и-двумя-способами"],
)
def test_translit_сводит_разнобой_написания_к_одной_форме(
    first: str, second: str
) -> None:
    assert translit_to_cyrillic(first) == translit_to_cyrillic(second)


@pytest.mark.parametrize(
    "latin, expected",
    [
        ("Kak proverit balans", "как проверит баланс"),
        ("pochemu", "почему"),
        ("schet", "счет"),
        ("svoy nomer", "свой номер"),
        ("muzei", "музей"),
        ("eshche", "еще"),
    ],
    ids=["фраза", "почему", "сч-не-щ", "й-после-гласной", "конечное-i", "щ"],
)
def test_translit_русской_латиницы(latin: str, expected: str) -> None:
    assert translit_to_cyrillic(latin) == expected


def test_translit_не_трогает_кириллицу() -> None:
    assert translit_to_cyrillic("Балансты кантип текшерем?") == (
        "балансты кантип текшерем"
    )


def test_translit_смешанного_текста() -> None:
    assert translit_to_cyrillic("SIM kartamdy бөгөттөө kerek") == (
        "сим картамды бөгөттөө керек"
    )


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "😀"])
def test_translit_пустого_текста(raw: str) -> None:
    assert translit_to_cyrillic(raw) == ""
