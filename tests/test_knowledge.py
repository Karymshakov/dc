"""Проверки базы знаний и слоя синонимов.

Тесты проверяют правила, а не реализацию: состав базы, нормализацию
и дисциплину наполнения ключевых слов. Последнее важнее всего: слой
синонимов — главный рычаг точности, и он же главный соблазн подогнать
решение под открытый набор.
"""
from __future__ import annotations

import csv

import pytest

from faq_bot import config
from faq_bot.knowledge import (
    HOMONYM_CONTEXT,
    KEYWORDS,
    build_fields,
    content_terms,
    content_text,
    default_faq,
    distinctive_terms,
    fold,
    is_generic,
    load_faq,
    missing_keywords,
    transliterate,
)


@pytest.fixture(scope="module")
def entries():
    return default_faq()


def test_база_содержит_пятнадцать_стабильных_тем(entries):
    assert len(entries) == config.FAQ_SIZE
    assert [entry.id for entry in entries] == list(range(1, config.FAQ_SIZE + 1))


def test_у_каждой_темы_есть_слой_синонимов(entries):
    assert missing_keywords(entries) == []
    for entry in entries:
        assert len(entry.keywords) >= 5, f"тема {entry.id} почти без синонимов"


def test_синонимы_есть_на_обоих_языках(entries):
    """В каждой теме должны быть и русские, и кыргызские формы.

    Иначе тема находится только на одном языке, а ТЗ требует двух.
    """
    kyrgyz_letters = set("өүңһ")
    for entry in entries:
        kyrgyz = [word for word in entry.keywords
                  if set(word) & kyrgyz_letters
                  or any(part in word for part in ("кантип", "канча", "акча",
                                                   "номерим", "кыз", "жок",
                                                   "сим карта", "калдыгы",
                                                   "жазылуу", "эсеп", "байланыш",
                                                   "чыгым", "тариф", "детализация",
                                                   "интернет", "оператор", "apn",
                                                   "гб", "роуминг"))]
        assert kyrgyz, f"тема {entry.id} без кыргызских синонимов"


def test_ключевые_слова_не_повторяются_между_темами():
    """Одна формулировка не может принадлежать двум темам.

    Повтор означает, что две темы спорят за один и тот же запрос, и выбор
    между ними становится случайным.
    """
    owners: dict[str, list[int]] = {}
    for faq_id, words in KEYWORDS.items():
        for word in words:
            owners.setdefault(content_text(word, expand_latin=False), []).append(faq_id)
    collisions = {text: ids for text, ids in owners.items()
                  if len(set(ids)) > 1}
    assert not collisions, f"одинаковые ключевые слова у разных тем: {collisions}"


#: Ключевые слова, которые целиком лежат внутри размеченного вопроса dev
#: и всё же остаются. Для каждого решение принято отдельно: это термин
#: предметной области, который нельзя не написать, а не память о строке.
#:
#: Проверка здесь строгая (все значимые слова ключа есть в вопросе, порядок
#: не важен), поэтому в неё попадает и честная лексика: если размеченный
#: вопрос сам состоит из одного термина — «Эсепке акча салуу» — то любое
#: верное ключевое слово этой темы окажется его подмножеством. Отказываться
#: от термина ради зелёного теста было бы хуже подгонки: решение перестало
#: бы работать и на скрытом наборе тоже.
ALLOWED_OVERLAPS: dict[str, str] = {
    "инет": "разговорное имя интернета, одно слово; в вопросе Q05 это 25% текста",
    "роуминг": "название самой темы, стоит в question_ru базы",
    "apn": "технический термин из ответа базы",
    "точка доступа": "русское название APN, стандартный термин настроек",
    "жазылууну өчүрүү": "кыргызское действие темы 8, то же, что question_ky базы",
    "акча салуу": "словарное «положить деньги»; Q22 сам состоит из этого термина",
}


def _dev_questions() -> list[tuple[str, frozenset[str]]]:
    """Размеченные вопросы открытого набора в поисковой нормализации."""
    with config.DEV_PATH.open(encoding="utf-8-sig", newline="") as handle:
        rows = [(("" + (row.get("id") or "")).strip(),
                 content_text(row.get("text") or "", expand_latin=False))
                for row in csv.DictReader(handle)]
    return [(case_id, frozenset(text.split())) for case_id, text in rows if text]


def test_ключевые_слова_не_копируют_вопросы_открытого_набора():
    """Защита от подгонки под размеченный набор.

    Синонимы — это знание предметной области: «балик», «точка доступа»,
    «*100#». Копия размеченной формулировки даёт красивую цифру на открытом
    наборе и ничего не даёт на скрытом, поэтому запрещена.

    Проверка идёт по ВСЕМУ questions_dev.csv, а не по tune: holdout
    вычисляется вычитанием tune из dev, и копия строки, не попавшей в tune,
    такая же подгонка. И не только по полному совпадению строки: дословный
    фрагмент («хочу другой тариф» внутри вопроса) — та же память о строке,
    а переставленные слова («потерял симку» против «симку потерял») её не
    оправдывают. Поэтому ключевое слово нарушает правило, если ВСЕ его
    значимые слова содержатся в одном размеченном вопросе.

    Осознанные исключения перечислены в ALLOWED_OVERLAPS с обоснованием
    по каждому.
    """
    if not config.DEV_PATH.exists():
        pytest.skip("questions_dev.csv недоступен")
    questions = _dev_questions()
    offenders: list[str] = []
    for faq_id, words in sorted(KEYWORDS.items()):
        for word in words:
            if word in ALLOWED_OVERLAPS:
                continue
            key = frozenset(content_text(word, expand_latin=False).split())
            if not key:
                continue
            inside = [case_id for case_id, question in questions
                      if key <= question]
            if inside:
                offenders.append(f"тема {faq_id}: {word!r} целиком в {inside}")
    assert not offenders, (
        "ключевое слово повторяет размеченный вопрос (подгонка): "
        + "; ".join(offenders))


def test_список_разрешённых_пересечений_не_устарел():
    """Исключение живёт, только пока живёт само ключевое слово.

    Иначе список исключений однажды начнёт разрешать то, чего уже нет,
    и перестанет быть обозримым.
    """
    alive = {word for words in KEYWORDS.values() for word in words}
    stale = sorted(set(ALLOWED_OVERLAPS) - alive)
    assert not stale, f"разрешение выдано удалённым ключевым словам: {stale}"
    assert all(reason for reason in ALLOWED_OVERLAPS.values()), (
        "каждое исключение обязано иметь обоснование")


def test_общие_корни_не_проглатывают_опознаватели_тем():
    """Список общих корней не имеет права съесть слово, выбирающее тему.

    Общий корень лишает слово права ПОДТВЕРЖДАТЬ тему. Если в список
    попадёт «кошумча» или «баланс», подтверждать тему станет нечем и
    верные вопросы начнут отказываться. Поэтому проверяются и опознаватели
    тем, и слова, из-за которых префикс мог бы оказаться слишком жадным:
    «цен» проглотил бы «центр», «биле» — «билет», «кош» — «кошумча».
    """
    for word in ("баланс", "балансты", "балик", "роуминг", "тариф", "тарифти",
                 "интернет", "инет", "кошумча", "симка", "симку", "детализация",
                 "apn", "жазылуу", "номер", "номерим", "пакет", "трафик", "гб",
                 "оператор", "центр", "билет", "жоголду", "уурдалды", "калдыгы"):
        assert not is_generic(word), f"{word!r} выбирает тему, он не общий корень"


def test_общий_корень_опознаётся_в_любой_словоформе():
    """Агглютинация и русская морфология не должны прятать общий корень."""
    for word in ("работает", "работаешь", "работа", "купить", "куплю",
                 "покупка", "подключить", "подключите", "получить",
                 "проверить", "узнать", "стоит", "стоимость",
                 "деньги", "денег", "акча", "акчаны",
                 "иштебейт", "иштейт", "текшерем", "сатып"):
        assert is_generic(word), f"{word!r} сам по себе тему не выбирает"


def test_различающие_слова_это_вопрос_без_скелета_и_общих_корней():
    """Остаётся то, чем вообще можно выбрать тему."""
    terms = ["/".join(term) for term in distinctive_terms("Как купить симку?")]
    assert terms == ["симку"]
    # Вопрос, в котором нет ни одного различающего слова, выбрать тему
    # не может: «работает» бывает у интернета, у поддержки и у самого бота.
    assert distinctive_terms("как это работает") == []
    assert distinctive_terms("") == []
    assert distinctive_terms("Как проверить баланс?") == [("баланс",)]


def test_омоним_рядом_со_спутником_не_подтверждает_тему():
    """«Эсеп» — это и счёт абонента, и счёт в игре.

    Рядом с «футбол», «матч», «оюн» слово означает игру и выбирать тему
    не может. Законный вопрос про счёт абонента спутника не содержит
    и ничего не теряет.
    """
    for text in ("Футболдун эсеби кандай?", "Какой счёт в матче?",
                 "Оюндун эсеби канча?", "какой счет у игры"):
        assert distinctive_terms(text) and not any(
            variant.startswith(("эсеп", "эсеб", "счет"))
            for term in distinctive_terms(text) for variant in term), text
    for text in ("Эсебимде канча сом калды?", "Эсепке акча салуу",
                 "счет на нуле", "состояние счета"):
        assert any(variant.startswith(("эсеп", "эсеб", "счет"))
                   for term in distinctive_terms(text) for variant in term), text


def test_спутника_омонима_нет_в_базе_оператора():
    """Иначе список спутников отнимет подтверждение у законного вопроса.

    Это и граница между предметным знанием и чёрным списком внецелевых
    тем: спутник разрешён только тогда, когда оператор такого слова
    не говорит вовсе.
    """
    index = {word for field in build_fields(default_faq())
             for word in field.text.split()}
    for ambiguous, context in HOMONYM_CONTEXT:
        assert ambiguous, "омоним без корней"
        collisions = sorted(word for word in index
                            if word.startswith(context))
        assert not collisions, (
            f"спутник омонима {ambiguous} встречается в базе: {collisions}")


def test_нормализация_выбрасывает_мусор_и_хранит_коды():
    assert fold("") == ""
    assert fold("   \n\t  ") == ""
    assert fold("!!! ??? ...") == ""
    assert fold("Как проверить баланс?") == "как проверить баланс"
    # Короткие коды — опознаваемые токены темы, их нельзя терять.
    assert "*100#" in fold("Наберите *100#.")
    assert "*145*" in fold("*145*номер*сумма#")


def test_нормализация_сворачивает_кыргызские_буквы():
    """«көрөм» и «korom» обязаны сойтись в одной записи."""
    assert fold("көрөм") == fold("корom".replace("om", "ом")) == "кором"
    assert fold("Жоголгон SIM") == "жоголгон sim сим"
    assert transliterate("balansymdy") == "балансымды"
    assert "балансымды" in fold("Balansymdy kantip korom")


def test_латиница_и_кириллица_один_термин():
    """Для токенного метода «korom» и «кором» — одно слово, а не два."""
    terms = content_terms("Balansymdy korom")
    assert all(isinstance(term, tuple) for term in terms)
    assert any("балансымды" in term and "balansymdy" in term for term in terms)


def test_служебные_слова_выброшены():
    assert content_text("как у меня") == ""
    assert content_text("Как проверить баланс?") == "проверить баланс"
    assert content_text("Балансты кантип текшерем?") == "балансты текшерем"
    # Отрицание — это суть темы «интернет не работает», его не выбрасываем.
    assert "не" in content_text("Интернет не работает")


def test_индекс_покрывает_все_темы(entries):
    fields = build_fields(entries)
    assert {field.faq_id for field in fields} == {entry.id for entry in entries}
    assert all(field.text.strip() for field in fields)
    assert all(0.0 < field.weight <= 1.0 for field in fields)


def test_загрузка_из_файла_совпадает_с_базой_по_умолчанию(entries):
    assert load_faq(config.FAQ_PATH) == entries
