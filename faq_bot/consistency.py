"""Проверка фактической согласованности ответа перед отправкой.

Метод перенесён из нашей работы «Контроль фактической согласованности ответов
языковой модели в настраиваемой системе AXON CRM». Там измерено на двухстах
черновиках с известной правдой:

    D (правила на связях)   полнота 0,950   52 мс   без обращения к модели
    P (NeMo Guardrails)     полнота 0,540  885 мс   одно обращение
    A (проверка по списку значений) полнота 0,227

Главный довод той работы: при подмене СВЯЗИ — когда две верные суммы поменяны
местами между категориями — проверка «есть ли это число в источнике» даёт
0 из 15, а проверка связи 15 из 15. Число настоящее, неверна привязка.

Здесь тот же случай в миниатюре. Бот не сочиняет текст, он цитирует faq.json,
поэтому выдумать факт он не может. Но он может выдать РЕАЛЬНЫЙ факт от чужой
темы: код *111# существует, однако принадлежит доп. интернету, а не балансу.
Для абонента это хуже выдумки — он наберёт работающую команду и получит не то.

Проверяется не наличие значения в базе, а его принадлежность выбранной теме:

    факт в ответе  ->  он есть в записи faq.json с ЭТИМ id ?
                       нет -> ответ несогласован, отдаём дословный текст темы

Стоимость — микросекунды и ноль обращений к модели. Это сознательный выбор
той же работы: контроль, который дороже самой генерации, на практике не живёт.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import FaqEntry

# USSD-команда: звёздочка, цифры и разделители, решётка на конце.
USSD_PATTERN = re.compile(r"\*[\d*#]*\d[\d*]*#")

# Денежная сумма с единицей. Единица обязательна: «100» само по себе может
# быть чем угодно, «100 сом» — уже утверждение о цене.
MONEY_PATTERN = re.compile(r"\b(\d[\d\s]{0,8})\s*(сом|сому|сомго|с\.)\b",
                           re.IGNORECASE)

# Объём трафика: «5 ГБ», «1 гб», «500 МБ».
VOLUME_PATTERN = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(гб|мб|gb|mb)\b",
                            re.IGNORECASE)

# Телефонный номер оператора или короткий сервисный номер.
PHONE_PATTERN = re.compile(r"\b(?:\+?996\d{9}|\d{3,4})\b")


@dataclass(frozen=True)
class Fact:
    """Проверяемое утверждение, извлечённое из текста ответа."""

    kind: str      # "ussd" | "money" | "volume"
    value: str     # нормализованное представление

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass
class Verdict:
    """Результат проверки. Несогласованный ответ отправлять нельзя."""

    consistent: bool
    checked: tuple[Fact, ...] = ()
    violations: tuple[Fact, ...] = ()
    reason: str = ""

    def explain(self) -> str:
        """Человеческое объяснение для лога и для демонстрации жюри."""
        if self.consistent:
            return (f"согласован: проверено фактов {len(self.checked)}"
                    if self.checked else "согласован: проверяемых фактов нет")
        listed = ", ".join(str(item) for item in self.violations)
        return f"НЕ согласован: {self.reason} ({listed})"


def _normalize_number(raw: str) -> str:
    """Убрать пробелы внутри числа и привести запятую к точке."""
    return raw.replace(" ", "").replace(" ", "").replace(",", ".")


def extract_facts(text: str) -> tuple[Fact, ...]:
    """Вытащить из текста утверждения, которые можно проверить по базе.

    Проверяемым считается только то, по чему абонент будет действовать:
    команда, которую он наберёт, цена, которую он ожидает, объём, который
    купит. Обычные слова не проверяются — их согласованность обеспечена
    тем, что текст целиком взят из faq.json.
    """
    facts: list[Fact] = []

    for match in USSD_PATTERN.finditer(text):
        facts.append(Fact("ussd", match.group(0)))

    for number, _unit in MONEY_PATTERN.findall(text):
        facts.append(Fact("money", _normalize_number(number)))

    for number, unit in VOLUME_PATTERN.findall(text):
        facts.append(Fact("volume", f"{_normalize_number(number)}{unit.lower()}"))

    # Порядок сохраняем, дубли убираем: один и тот же код в вопросе и
    # в ответе — одно утверждение, а не два.
    seen: set[tuple[str, str]] = set()
    unique: list[Fact] = []
    for fact in facts:
        key = (fact.kind, fact.value.lower())
        if key not in seen:
            seen.add(key)
            unique.append(fact)
    return tuple(unique)


def entry_facts(entry: FaqEntry) -> tuple[Fact, ...]:
    """Все проверяемые факты, принадлежащие теме.

    Берутся из обоих языков записи: кыргызский и русский ответы describe
    одну услугу, и код в них один и тот же.
    """
    source = " ".join((entry.question_ru, entry.question_ky,
                       entry.answer_ru, entry.answer_ky))
    return extract_facts(source)


def check(answer_text: str, entry: FaqEntry) -> Verdict:
    """Принадлежат ли факты ответа именно этой теме.

    Возвращает вердикт, а не бросает исключение: решение, что делать
    с несогласованным ответом, принимает вызывающий код.
    """
    claimed = extract_facts(answer_text)
    if not claimed:
        return Verdict(consistent=True, reason="проверяемых фактов нет")

    allowed = {(fact.kind, fact.value.lower()) for fact in entry_facts(entry)}
    violations = tuple(
        fact for fact in claimed
        if (fact.kind, fact.value.lower()) not in allowed
    )

    if violations:
        kinds = sorted({fact.kind for fact in violations})
        return Verdict(
            consistent=False,
            checked=claimed,
            violations=violations,
            reason=f"факт не принадлежит теме {entry.id} ({', '.join(kinds)})",
        )
    return Verdict(consistent=True, checked=claimed)


def safe_answer(answer_text: str, entry: FaqEntry, language: str) -> tuple[str, Verdict]:
    """Текст, который можно отправить абоненту, и вердикт проверки.

    Несогласованный ответ заменяется дословным текстом темы из faq.json.
    Это и есть поведение из той работы: при срабатывании правила черновик
    не отправляется, а заменяется ответом, собранным из источника.
    """
    verdict = check(answer_text, entry)
    if verdict.consistent:
        return answer_text, verdict
    return entry.answer(language), verdict


def audit_knowledge(entries: list[FaqEntry]) -> dict[str, list[str]]:
    """Какие факты каким темам принадлежат — и где они пересекаются.

    Факт, встречающийся в двух темах, проверкой связи не ловится: он
    законен в обеих. Такие места надо знать заранее, поэтому они
    перечисляются отдельно. Это прямой аналог «класса, который ловится
    хуже всех» из той работы — слабое место называется, а не замалчивается.
    """
    owners: dict[str, list[str]] = {}
    for entry in entries:
        for fact in entry_facts(entry):
            owners.setdefault(str(fact), []).append(str(entry.id))
    return {fact: ids for fact, ids in owners.items() if len(ids) > 1}
