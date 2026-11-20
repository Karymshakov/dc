"""Определение языка вопроса: «ru» или «ky». Третьего значения нет.

Контракт (``contracts.LanguageDetector``): :meth:`RuleLanguageDetector.detect`
всегда возвращает ``"ru"`` или ``"ky"``, не бросает исключений, на пустой
строке даёт ``"ru"``.

Как устроено решение
--------------------

Язык не угадывается по «преобладанию букв»: в обоих языках кириллица, а
кыргызскую фразу часто пишут латиницей. Вместо этого собираются **улики
за каждый язык**, и побеждает тот, у кого их больше. Ничья — ``"ru"``
(значение по умолчанию из уточнений организаторов).

Так прямо выполняется правило раздела 5: «базовый язык смешанной фразы
определяется языком грамматической основы, бренд/русский термин не меняет
кыргызскую основу». Один заимствованный термин — ноль улик: слова
«баланс», «интернет», «тариф», «роуминг» нейтральны и не голосуют.

Улики, по убыванию надёжности:

1. **Буквы ң, ө, ү** — вне кыргызского не встречаются.
2. **Служебные слова** обоих языков: «кантип», «канча», «жана», «үчүн»
   против «как», «что», «сколько», «на». Закрытый класс, он не меняется
   от темы вопроса.
3. **Морфология.** Кыргызский агглютинативен, и окончание угадать проще,
   чем слово целиком. Чтобы русские слова не ловились на «-ды» и «-ти»,
   кыргызское окончание принимается только при **сингармонизме**: гласная
   окончания обязана соответствовать последней гласной основы, а д/г
   против т/к — звонкости последнего согласного. «Погоды» отбрасывается
   (после «о» кыргызский требует «-ду»), «балансты» принимается.
4. **Латиница.** Слово транслитерируется в кириллицу и проверяется теми
   же правилами. Разнобой написания (``kantip``/``kantıp``,
   ``jatat``/``zhatat``, ``ishtebei``/``ishtebey``) сводится к одной
   форме самой транслитерацией, поэтому второй, латинский словарь не
   нужен: в списке ``LATIN_KY_MARKERS`` остались считанные написания,
   которых транслитерация не достаёт. Сингармонизм для латиницы
   проверяется мягко (пишут на слух, «almashtyrgim» вместо
   «алмаштыргым»), а вес такой улики понижен.
   Русская латиница («kak proverit balans») остаётся русской, потому
   что улики считаются после транслитерации для обоих языков сразу.
5. **ь, ъ, щ** — в кыргызском их нет.

Все веса подобраны по открытому набору `tune.csv` и по собственным
свежим парафразам; `holdout.csv` при настройке не открывался.
"""
from __future__ import annotations

from dataclasses import dataclass

from faq_bot.normalize import fold, normalize, translit_to_cyrillic

__all__ = [
    "RuleLanguageDetector",
    "LanguageVerdict",
    "detect",
    "detect_language",
    "DETECTOR",
]

RU = "ru"
KY = "ky"

# ---------------------------------------------------------------------------
# Веса улик
# ---------------------------------------------------------------------------

W_KY_LETTER = 4.0        # ң, ө, ү — сильнее любого отдельного слова
W_KY_LETTER_EXTRA = 1.0  # каждое следующее слово с такой буквой
W_WORD = 3.0             # служебное слово из закрытого списка
W_SUFFIX = 2.0           # окончание, прошедшее проверку сингармонизма
W_SUFFIX_LATIN = 1.5     # то же для латиницы: написание на слух
W_WEAK_SUFFIX = 1.0      # окончания, пересекающиеся с русскими
W_RU_LETTER = 3.0        # щ, ъ
W_SOFT_SIGN = 2.0        # ь

# ---------------------------------------------------------------------------
# Словари служебных и частых слов
# ---------------------------------------------------------------------------

# Слова сравниваются в «сглаженной» форме (fold): ң→н, ө→о, ү→у, без ь.
# Благодаря этому латинское «uchun» встречает «үчүн», а «korom» — «көрөм».

_KY_WORDS_SOURCE = """
кантип кантып канча канчага канчадан канчалык кандай кандайча
кайсы кайда кайдан качан
эмне эмнеге неге жана менен үчүн аркылуу чейин кийин боюнча жөнүндө
керек керекпи болобу болот болду болсо болгон болбойт барбы жокпу
мүмкүн эгер анан эле гана дагы тууралуу кыргызча орусча кечиресиз
маалымат жатат жатам жатабы
келет келди келсе алам аласыз алсам алгым алып билем билгим билсем
кылам кылсам кошом кошкум көрөм көргүм котором текшерем текшерүү
иштейт иштебейт иштебей иштеп калды калган калдык калдыгын
бар жок эмес мен менин мага меники сен сиз сизде силер бул ошол
өзүм өзүмдүн акча эсеп эсепке акысыз жардам кызмат сураныч суроо
салам рахмат ооба жакшы жаңы бир эки үч көп азыр бүгүн эртең
жеке колдоо кайра туура мүнөт күн баасы
"""

_RU_WORDS_SOURCE = """
как что чего чем где когда куда откуда почему зачем сколько
какой какая какое какие каким какую который которая которые
можно нельзя нужно нужен нужна надо хочу хотел хотелось могу можете
мне меня мной мой моя мое мои моего моему мою твой ваш ваша ваше ваши
наш на в во за с со из от до по у к об о при для без про
не нет ни и или а но если чтобы это этот эта эти тот там тут здесь
вы я мы он она они его ее их им еще уже тоже также очень
пожалуйста спасибо скажите подскажите помогите здравствуйте привет
деньги денег счет счета счету стоит стоимость сделать делать работает
проверить узнать подключить отключить пополнить перевести сменить
поменять восстановить заблокировать настроить получить отправить
симка симки симку симкой
"""

# Заимствования и термины, общие для обоих языков. Они не голосуют:
# именно это и означает «бренд или русский термин не меняет основу».
_NEUTRAL_SOURCE = """
баланс интернет тариф роуминг сим sim apn апн sms смс гб gb мб mb
номер телефон оператор пакет трафик детализация комиссия кредит банк
терминал кабинет услуга wifi вайфай смска
"""

#: Русские падежные окончания, которые навешиваются на заимствование:
#: «роуминга», «тарифом», «интернете». Русская морфология на термине —
#: это улика за русский, в отличие от самого термина.
_RU_CASE_ENDINGS = frozenset(
    ("а", "у", "е", "ы", "и", "ом", "ем", "ов", "ев", "ами", "ями",
     "ах", "ях", "ой", "ею")
)


def _lexicon_key(word: str) -> str:
    """Ключ для поиска по словарю: сглаженная форма плюс э → е.

    Буква «э» в латинице не передаётся: «emne» транслитерируется в
    «емне», а в словаре живёт «эмне». Один общий ключ снимает весь
    этот класс расхождений — и для «esep», и для «eki», и для «emes».
    """
    return fold(word).replace("э", "е")


def _vocabulary(source: str) -> frozenset[str]:
    """Превратить список слов в множество сглаженных ключей."""
    return frozenset(_lexicon_key(word) for word in source.split())


KY_WORDS = _vocabulary(_KY_WORDS_SOURCE)
RU_WORDS = _vocabulary(_RU_WORDS_SOURCE)
NEUTRAL_WORDS = _vocabulary(_NEUTRAL_SOURCE)

# Латинские написания, до которых транслитерация не доводит. Список
# намеренно крошечный: из 39 проверенных латинских форм кыргызских слов
# 37 узнаются через транслитерацию и общий словарь, и держать здесь
# второй словарь было бы самообманом. Остаются варианты, где человек
# путает ш/ч («ushun» вместо «uchun») и пишет «ei» вместо «ey» перед
# согласной («ishtebeit» вместо «ishtebeyt»).
LATIN_KY_MARKERS = frozenset(
    ("ushun", "üshün", "ushün", "ishtebeit", "ishtebeyit")
)

# ---------------------------------------------------------------------------
# Сингармонизм
# ---------------------------------------------------------------------------

VOWELS = frozenset("аеиоуыэюяөү")
VOICELESS = frozenset("кпстфхцчшщ")

#: Какую узкую гласную (ы/и/у/ү) требует последняя гласная основы.
_HIGH_BY_STEM = {
    "а": "ы", "ы": "ы", "я": "ы",
    "е": "и", "и": "и", "э": "и",
    "о": "у", "у": "у", "ю": "у",
    "ө": "ү", "ү": "ү",
}

#: Какую широкую гласную (а/е/о/ө) требует последняя гласная основы.
_LOW_BY_STEM = {
    "а": "а", "ы": "а", "я": "а", "у": "а", "ю": "а",
    "е": "е", "и": "е", "э": "е",
    "о": "о",
    "ө": "ө", "ү": "ө",
}

#: Гласные переднего ряда — для имени действия «-уу / -үү».
_FRONT = frozenset("еиэөү")


@dataclass(frozen=True)
class _SuffixRule:
    """Правило для одного кыргызского окончания."""

    height: str | None   # "high" | "low" | "long" | None
    stem_end: str | None  # "voiceless" | "voiced" | "consonant" | "vowel"
    weight: float = W_SUFFIX


def _expand(
    shapes: tuple[str, ...],
    height: str | None,
    stem_end: str | None,
    weight: float = W_SUFFIX,
) -> dict[str, _SuffixRule]:
    """Развернуть список готовых вариантов окончания в таблицу правил."""
    return {shape: _SuffixRule(height, stem_end, weight) for shape in shapes}


KY_SUFFIXES: dict[str, _SuffixRule] = {
    # Винительный падеж и прошедшее время: китепти, балансты, калды.
    **_expand(("ды", "ди", "ду", "дү"), "high", "voiced"),
    **_expand(("ты", "ти", "ту", "тү"), "high", "voiceless"),
    # «-ны» и «-ин» слишком похожи на русские «страны», «бензин»,
    # поэтому их голос вдвое тише.
    **_expand(("ны", "ни", "ну", "нү"), "high", "vowel", W_WEAK_SUFFIX),
    # Винительный от притяжательной формы: пакетин, тарифин.
    **_expand(("ын", "ин", "ун", "үн"), "high", "consonant", W_WEAK_SUFFIX),
    # Родительный падеж: тарифтин, абоненттин.
    **_expand(("дын", "дин", "дун", "дүн"), "high", "voiced"),
    **_expand(("тын", "тин", "тун", "түн"), "high", "voiceless"),
    **_expand(("нын", "нин", "нун", "нүн"), "high", "voiced"),
    # Дательный: эсепке, номерге.
    **_expand(("га", "ге", "го", "гө"), "low", "voiced"),
    **_expand(("ка", "ке", "ко", "кө"), "low", "voiceless"),
    # Местный: кабинетте, балансымда.
    **_expand(("да", "де", "до", "дө"), "low", "voiced"),
    **_expand(("та", "те", "то", "тө"), "low", "voiceless"),
    # Исходный: кабинеттен, жеринен.
    **_expand(("дан", "ден", "дон", "дөн"), "low", "voiced"),
    **_expand(("тан", "тен", "тон", "төн"), "low", "voiceless"),
    **_expand(("нан", "нен", "нон", "нөн"), "low", "voiced"),
    # Множественное число: пакеттер, кызматтар, жазылуулары.
    **_expand(("лар", "лер", "лор", "лөр"), "low", "voiced"),
    **_expand(("лары", "лери", "лору", "лөрү"), None, None),
    **_expand(("дар", "дер", "дор", "дөр"), "low", "voiced"),
    **_expand(("тар", "тер", "тор", "төр"), "low", "voiceless"),
    # Притяжательные: номерим, интернетим, баасы.
    **_expand(("ым", "им", "ум", "үм"), "high", "consonant"),
    **_expand(("сы", "си", "су", "сү"), "high", "vowel"),
    # Настоящее-будущее 3 л.: иштейт, турат, келет, болот.
    **_expand(("ат", "ет", "от", "өт"), "low", "consonant"),
    **_expand(("йт",), None, "vowel"),
    # 1 л. ед. ч.: алам, билем, кошом — пересекается с русским
    # творительным падежом («телефоном»), поэтому вес понижен.
    **_expand(("ам", "ем", "ом", "өм"), "low", "consonant", W_WEAK_SUFFIX),
    **_expand(("мын", "мин", "мун", "мүн"), "high", "consonant"),
    # Желательное наклонение: алгым келет, өзгөрткүм келет.
    **_expand(("гым", "гим", "гум", "гүм"), "high", "voiced"),
    **_expand(("кым", "ким", "кум", "күм"), "high", "voiceless"),
    # Условное наклонение: которсом, алсам.
    **_expand(("сам", "сем", "сом", "сөм"), "low", "consonant"),
    # Причастие прошедшего времени: жоголгон, уурдалган.
    **_expand(("ган", "ген", "гон", "гөн"), "low", "voiced"),
    **_expand(("кан", "кен", "кон", "көн"), "low", "voiceless"),
    # Деепричастия: кылып, иштебей.
    **_expand(("ып", "ип", "уп", "үп"), "high", "consonant"),
    **_expand(("бай", "бей", "бой", "бөй"), "low", "voiced"),
    **_expand(("пай", "пей", "пой", "пөй"), "low", "voiceless"),
    # Имя действия: салуу, текшерүү.
    **_expand(("уу", "үү"), "long", "consonant"),
    # Относительная форма: кабинеттеги, үйдөгү.
    **_expand(("гы", "ги", "гу", "гү"), "high", "voiced"),
    # «Без чего-то»: акысыз, лимитсиз.
    **_expand(("сыз", "сиз", "суз", "сүз"), "high", None),
    # Вопросительная частица: болобу, барбы.
    **_expand(("бы", "би", "бу", "бү"), "high", "voiced"),
    **_expand(("пы", "пи", "пу", "пү"), "high", "voiceless"),
}

#: Длины окончаний от больших к меньшим: «дын» важнее, чем «ын».
_SUFFIX_LENGTHS = tuple(sorted({len(s) for s in KY_SUFFIXES}, reverse=True))

#: Русские окончания, которых в кыргызском не бывает. Намеренно не
#: включены «-ат», «-ет», «-ом», «-ды», «-ты»: они общие для двух языков.
RU_SUFFIXES: tuple[str, ...] = (
    "ться", "тся", "ешь", "ишь", "ете", "ите", "йте",
    "ают", "яют", "уют", "ует", "еют",
    "ция", "ции", "ние", "ния", "нии", "ение", "ость", "ости",
    "ами", "ями", "ыми", "ими", "ого", "его", "ому", "ему",
    "ть", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю",
    "ый", "ий", "ых", "их", "ов", "ев", "ли",
    # Прошедшее время: списали, подорожал, снимала.
    "ал", "ял", "ел", "ил", "ыл", "ул",
    "ала", "яла", "ела", "ила", "ула",
    "ало", "яло", "ело", "ило", "уло",
    "али", "яли", "ели", "или", "ули",
)
_RU_SUFFIX_LENGTHS = tuple(sorted({len(s) for s in RU_SUFFIXES}, reverse=True))

#: Минимальная длина основы, при которой окончание вообще что-то значит.
MIN_STEM = 3
#: Минимальная длина слова для проверки русских окончаний.
MIN_RU_WORD = 5


# ---------------------------------------------------------------------------
# Результат разбора
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageVerdict:
    """Решение вместе с уликами — нужно для отладки и отчётов."""

    language: str
    ky_score: float
    ru_score: float
    signals: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Детектор
# ---------------------------------------------------------------------------


class RuleLanguageDetector:
    """Правиловый определитель языка. Реализует протокол LanguageDetector."""

    def detect(self, text: str) -> str:
        """Вернуть «ru» или «ky». Пустой вопрос — «ru»."""
        return self.explain(text).language

    def explain(self, text: str) -> LanguageVerdict:
        """То же решение, но с разбором улик."""
        words = normalize(text).split()
        if not words:
            return LanguageVerdict(RU, 0.0, 0.0, ("пусто",))

        ky_score = 0.0
        ru_score = 0.0
        signals: list[str] = []
        letter_hits = 0

        for word in words:
            latin = _has_latin(word)
            cyrillic = translit_to_cyrillic(word) if latin else word
            key = _lexicon_key(cyrillic)

            if _has_ky_letter(cyrillic):
                letter_hits += 1
                gain = W_KY_LETTER if letter_hits == 1 else W_KY_LETTER_EXTRA
                ky_score += gain
                signals.append(f"буква ң/ө/ү: {word}")

            if key in NEUTRAL_WORDS:
                continue

            if key in KY_WORDS or (latin and _latin_marker(word)):
                ky_score += W_WORD
                signals.append(f"ky-слово: {word}")
                continue
            if key in RU_WORDS:
                ru_score += W_WORD
                signals.append(f"ru-слово: {word}")
                continue

            loan_case = _loan_case(key)
            if loan_case is not None:
                ru_score += W_SUFFIX
                signals.append(f"русский падеж термина -{loan_case}: {word}")
                continue

            suffix = _ky_suffix(cyrillic, relaxed=latin)
            if suffix is not None:
                name, weight = suffix
                if latin:
                    weight = min(weight, W_SUFFIX_LATIN)
                ky_score += weight
                signals.append(f"ky-окончание -{name}: {word}")
            elif latin and _latin_polite(word):
                ky_score += W_SUFFIX_LATIN
                signals.append(f"ky-латиница -ңыз: {word}")

            ru_suffix = _ru_suffix(cyrillic)
            if ru_suffix is not None:
                ru_score += W_SUFFIX
                signals.append(f"ru-окончание -{ru_suffix}: {word}")

            if "щ" in cyrillic or "ъ" in cyrillic:
                ru_score += W_RU_LETTER
                signals.append(f"буква щ/ъ: {word}")
            elif "ь" in cyrillic and ru_suffix is None:
                # Отдельная улика, только если окончание ещё не сработало:
                # «блокировать» не должно голосовать дважды.
                ru_score += W_SOFT_SIGN
                signals.append(f"мягкий знак: {word}")

        language = KY if ky_score > ru_score else RU
        return LanguageVerdict(language, ky_score, ru_score, tuple(signals))


DETECTOR = RuleLanguageDetector()


def detect(text: str) -> str:
    """Определить язык текста: «ru» или «ky»."""
    return DETECTOR.detect(text)


#: Говорящий псевдоним для вызова из других модулей.
detect_language = detect


# ---------------------------------------------------------------------------
# Вспомогательные проверки
# ---------------------------------------------------------------------------


def _has_latin(word: str) -> bool:
    """Есть ли в слове латинские буквы."""
    return any("a" <= character <= "z" for character in word)


def _has_ky_letter(word: str) -> bool:
    """Есть ли в слове ң, ө или ү."""
    return any(character in "ңөү" for character in word)


def _latin_marker(word: str) -> bool:
    """Слово целиком совпадает с латинским кыргызским маркером."""
    return word in LATIN_KY_MARKERS


#: Вежливое повеление 2 л. мн. ч. «-ңыз»: beringiz, teringiz, tandanyz.
#: Транслитерация даёт «нгиз», и обычные правила его не узнают.
_LATIN_POLITE = ("ngyz", "ngiz", "nguz", "ngüz", "nyz", "niz", "nuz", "nüz")
_LATIN_POLITE_MIN = 6


def _latin_polite(word: str) -> bool:
    """Кыргызское вежливое окончание, записанное латиницей."""
    return len(word) >= _LATIN_POLITE_MIN and word.endswith(_LATIN_POLITE)


def _loan_case(key: str) -> str | None:
    """Русское падежное окончание на общем термине: «роуминга», «тарифом»."""
    for stem in NEUTRAL_WORDS:
        if len(stem) >= 4 and key.startswith(stem):
            tail = key[len(stem):]
            if tail in _RU_CASE_ENDINGS:
                return tail
    return None


def _last_vowel(stem: str) -> str | None:
    """Последняя гласная основы."""
    for character in reversed(stem):
        if character in VOWELS:
            return character
    return None


def _stem_fits(stem: str, requirement: str | None) -> bool:
    """Проверить требование к последнему звуку основы."""
    if requirement is None:
        return True
    last = stem[-1]
    if requirement == "vowel":
        return last in VOWELS
    if requirement == "consonant":
        return last not in VOWELS
    if requirement == "voiceless":
        return last in VOICELESS
    if requirement == "voiced":
        return last not in VOICELESS
    return True


def _harmony_fits(stem_vowel: str, suffix: str, height: str | None) -> bool:
    """Проверить сингармонизм: гласная окончания против гласной основы."""
    if height is None:
        return True
    if height == "long":
        return suffix == ("үү" if stem_vowel in _FRONT else "уу")
    suffix_vowel = next(
        (character for character in suffix if character in VOWELS), ""
    )
    table = _HIGH_BY_STEM if height == "high" else _LOW_BY_STEM
    return table.get(stem_vowel, "") == suffix_vowel


def _ky_suffix(word: str, relaxed: bool = False) -> tuple[str, float] | None:
    """Найти кыргызское окончание слова.

    Возвращает пару (окончание, вес) или ``None``. При ``relaxed=True``
    сингармонизм не проверяется: так пишут латиницей, на слух
    (``almashtyrgim`` вместо «алмаштыргым»).
    """
    for length in _SUFFIX_LENGTHS:
        if len(word) <= length:
            continue
        suffix = word[-length:]
        rule = KY_SUFFIXES.get(suffix)
        if rule is None:
            continue
        stem = word[:-length]
        if len(stem) < MIN_STEM:
            continue
        stem_vowel = _last_vowel(stem)
        if stem_vowel is None:
            continue
        if not _stem_fits(stem, rule.stem_end):
            continue
        if not relaxed and not _harmony_fits(stem_vowel, suffix, rule.height):
            continue
        return suffix, rule.weight
    return None


def _ru_suffix(word: str) -> str | None:
    """Найти русское окончание слова."""
    if len(word) < MIN_RU_WORD:
        return None
    for length in _RU_SUFFIX_LENGTHS:
        suffix = word[-length:]
        if suffix in RU_SUFFIXES and len(word) - length >= MIN_STEM:
            return suffix
    return None
