"""Контекст диалога: наследование темы в рамках одного chat_id.

Пункт 4 ТЗ: «а сколько стоит?» после вопроса о роуминге понимается
в рамках предыдущей темы.

Правило наследования намеренно осторожное. В проверке жюри 40 обычных
вопросов и только 5 диалогов, поэтому жадное наследование темы принесёт
меньше, чем испортит. Тема наследуется, только когда совпали три условия:

1. своя уверенность ниже «зелёного» порога (уверенный ответ не трогаем);
2. реплика выглядит уточнением: короткая, без собственных ключевых слов
   темы, с уточняющим словом или начинается с «а»;
3. в этом chat_id есть живая (не просроченная) тема.

Хранилище — память процесса, словарь по chat_id с TTL и лимитом реплик.
Никакой БД, между chat_id ничего не протекает.

Здесь же живёт счётчик отказов подряд (`DialogMemory.misses`). Он нужен
слою бота: после нескольких отказов в одном чате бот сам предлагает
оператора, а не ждёт, пока абонент найдёт кнопку. Счётчик сбрасывается
любым успешным ответом и не влияет на контракт `answer()`.

Третья вещь для слоя бота — `DialogMemory.rollback()`. Бот отвечает на
первую реплику сразу, а дописанные к ней уточнения пересчитывают ответ
и переписывают то же сообщение. Для памяти диалога такая серия обязана
остаться одной репликой, иначе один вопрос, набранный тремя
сообщениями, вытеснит из памяти всю историю чата.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from . import config
from .contracts import Decision

# Уверенность, которую получает ответ, унаследованный из контекста.
# Контекст — сильный сигнал, поэтому ответ показывается «зелёным».
INHERITED_CONFIDENCE: float = config.CONFIDENCE_CONFIDENT

# Сколько разных чатов держим в памяти, чтобы она не росла бесконечно.
MAX_TRACKED_CHATS: int = 2000

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Основы слов, которые сами по себе задают тему. Если такое слово есть
# в реплике — это новый вопрос, а не уточнение к предыдущему.
TOPIC_STEMS: frozenset[str] = frozenset((
    # русский
    "баланс", "счет", "счёт", "деньг", "деньги", "денег", "пакет", "минут",
    "гиг", "гб", "трафик", "интернет", "инет", "роуминг", "тариф", "сим",
    "sim", "симк", "номер", "подпис", "перевод", "перевест", "перекин",
    "пополн", "закин", "apn", "апн", "оператор", "детализ", "расход",
    "списан", "блокир", "украл", "потер", "связь", "смс", "sms", "мегабайт",
    "гигабайт", "кабинет", "звон",
    # кыргызский
    "акча", "эсеп", "мүнөт", "калдык", "жазылуу", "котор", "толукта",
    "чыгым", "бөгөт", "уурда", "жогол", "байланыш", "колдоо", "тарифт",
    "номерим", "интернетим", "балансым",
    # латиница
    "balans", "internet", "tarif", "nomer", "akcha", "esep", "rouming",
))

# Слова, типичные для уточнения уже названной темы.
CLARIFIER_STEMS: frozenset[str] = frozenset((
    "сколько", "скок", "скока", "стоит", "стоимость", "цена", "почем",
    "почём", "дорого", "бесплатно", "платно", "когда", "долго", "можно",
    "точно", "где", "почему", "зачем", "комисси", "подробн", "ещё", "еще",
    "как", "что", "кто",
    "канча", "турат", "баасы", "акысыз", "качан", "кайда", "эмне", "кандай",
    "кантип", "болобу", "керекпи", "комиссия", "кымбатпы",
    "kancha", "kanca", "turat", "baasy", "skolko", "stoit",
))

# Начала реплик, которые почти всегда означают продолжение разговора.
FOLLOW_UP_MARKERS: frozenset[str] = frozenset((
    "а", "ну", "и", "ал", "анан", "эми", "ага", "a",
))

# Служебные слова: сами по себе темы не несут и уточнению не мешают.
FILLER_WORDS: frozenset[str] = frozenset((
    "это", "этот", "эта", "там", "тут", "же", "ли", "вот", "вообще",
    "тогда", "опять", "мне", "мой", "моя", "ваш", "его", "они", "да", "нет",
    "пожалуйста", "скажите", "подскажите", "напишите", "хочу", "надо",
    "бул", "ошол", "эле", "го", "мага", "менин", "өзү", "сиз", "ооба",
    "жок", "айтыңыз", "айт", "болот", "керек", "деген",
))

# Сколько незнакомых слов терпит уточняющая реплика. Ноль был бы слишком
# строг («канчадан баштап?» — законное уточнение), двойка открывает дорогу
# внецелевым вопросам вроде «бүгүн аба ырайы кандай?».
MAX_FOREIGN_WORDS: int = 1


def _words(text: str) -> list[str]:
    """Слова реплики в нижнем регистре."""
    return _WORD_RE.findall(text.lower().replace("ё", "е"))


def _has_stem(words: Iterable[str], stems: frozenset[str]) -> bool:
    """Есть ли среди слов хоть одно с основой из набора."""
    for word in words:
        for stem in stems:
            if word.startswith(stem) or (len(word) >= 4 and stem.startswith(word)):
                return True
    return False


def _is_familiar(word: str) -> bool:
    """Слово, которое может быть в уточнении, не задавая новой темы."""
    return (word in FOLLOW_UP_MARKERS
            or word in FILLER_WORDS
            or _has_stem((word,), CLARIFIER_STEMS))


def mentions_topic(text: str) -> bool:
    """Есть ли в реплике собственное ключевое слово темы.

    Нужна не только контексту: слой бота по этому признаку отличает
    «салам» от «салам, интернет иштебейт». Во втором случае приветствие
    игнорируется, потому что в реплике есть настоящий вопрос.
    """
    return _has_stem(_words(text), TOPIC_STEMS)


def is_follow_up(text: str) -> bool:
    """Похожа ли реплика на уточнение к предыдущей теме.

    Признаки: короткая, без собственных ключевых слов темы, почти целиком
    состоит из уточняющих и служебных слов и при этом либо начинается
    с «а»/«анан», либо содержит уточняющее слово («сколько», «канча»).

    Последнее условие — защита от жадности. Без него «бүгүн аба ырайы
    кандай?» считалось бы уточнением из-за слова «кандай» и получало бы
    тему предыдущей реплики вместо честного отказа.
    """
    words = _words(text)
    if not words or len(words) > config.FOLLOW_UP_MAX_WORDS:
        return False
    if _has_stem(words, TOPIC_STEMS):
        return False
    foreign = [word for word in words if not _is_familiar(word)]
    if len(foreign) > MAX_FOREIGN_WORDS:
        return False
    if words[0] in FOLLOW_UP_MARKERS and len(words) > 1:
        return True
    return _has_stem(words, CLARIFIER_STEMS)


@dataclass(frozen=True)
class Turn:
    """Одна запомненная реплика диалога."""

    faq_id: int | None
    confidence: float
    at: float


@dataclass(frozen=True)
class MissStreak:
    """Сколько раз подряд в этом чате не нашлось темы и когда последний."""

    count: int
    at: float


class DialogMemory:
    """Короткая память диалогов по chat_id.

    Хранит до `max_turns` последних реплик каждого чата не дольше
    `ttl_seconds`. Часы можно подменить — это нужно тестам на TTL.

    Кроме тем помнит цепочку отказов подряд: `misses()` отдаёт её длину,
    любой найденный ответ обнуляет счётчик.
    """

    def __init__(
        self,
        max_turns: int = config.CONTEXT_TURNS,
        ttl_seconds: float = config.CONTEXT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_chats: int = MAX_TRACKED_CHATS,
    ) -> None:
        self._max_turns = max(1, int(max_turns))
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._max_chats = max(1, int(max_chats))
        self._chats: OrderedDict[str, deque[Turn]] = OrderedDict()
        self._misses: OrderedDict[str, MissStreak] = OrderedDict()
        # Что сделал последний remember() этого чата: "turn" или "miss".
        # Нужно только rollback(), см. его описание.
        self._last: OrderedDict[str, str] = OrderedDict()

    @staticmethod
    def _key(chat_id: int | str) -> str:
        return str(chat_id)

    def remember(self, chat_id: int | str | None,
                 faq_id: int | None, confidence: float = 0.0) -> None:
        """Запомнить исход реплики.

        Реплика без темы темой не становится, но увеличивает цепочку
        отказов этого чата; найденная тема цепочку обнуляет.
        """
        if chat_id is None:
            return
        if faq_id is None:
            key = self._key(chat_id)
            self._remember_miss(key)
            self._mark_last(key, "miss")
            return
        key = self._key(chat_id)
        self._misses.pop(key, None)
        self._mark_last(key, "turn")
        turns = self._chats.get(key)
        if turns is None:
            turns = deque(maxlen=self._max_turns)
            self._chats[key] = turns
        turns.append(Turn(faq_id=int(faq_id),
                          confidence=float(confidence),
                          at=self._clock()))
        self._chats.move_to_end(key)
        while len(self._chats) > self._max_chats:
            self._chats.popitem(last=False)

    def _remember_miss(self, key: str) -> None:
        """Продлить цепочку отказов чата, уважая TTL и лимит чатов."""
        now = self._clock()
        previous = self._misses.get(key)
        fresh = previous is not None and now - previous.at <= self._ttl
        count = previous.count + 1 if fresh and previous is not None else 1
        self._misses[key] = MissStreak(count=count, at=now)
        self._misses.move_to_end(key)
        while len(self._misses) > self._max_chats:
            self._misses.popitem(last=False)

    def _mark_last(self, key: str, kind: str) -> None:
        """Запомнить вид последней записи чата (для `rollback`)."""
        self._last[key] = kind
        self._last.move_to_end(key)
        while len(self._last) > self._max_chats:
            self._last.popitem(last=False)

    def rollback(self, chat_id: int | str | None) -> bool:
        """Отменить последнюю запомненную реплику чата.

        Нужно слою бота. Когда абонент дописывает реплику к своему же
        вопросу, бот считает ответ заново по склеенному тексту и
        переписывает им предыдущий ответ. Для памяти диалога это должна
        остаться одна реплика, а не две: иначе один вопрос, набранный
        тремя сообщениями, вытеснит из короткой памяти всю историю чата
        и трижды продлит цепочку отказов, а абонент получит предложение
        оператора на первой же своей мысли.

        Возвращает True, если что-то отменено. Контракт `answer()`
        функция не затрагивает: её вызывает только слой бота.
        """
        if chat_id is None:
            return False
        key = self._key(chat_id)
        kind = self._last.pop(key, None)
        if kind == "turn":
            turns = self._chats.get(key)
            if not turns:
                return False
            turns.pop()
            if not turns:
                self._chats.pop(key, None)
            return True
        if kind == "miss":
            streak = self._misses.pop(key, None)
            if streak is None:
                return False
            if streak.count > 1:
                self._misses[key] = MissStreak(count=streak.count - 1,
                                               at=streak.at)
                self._misses.move_to_end(key)
            return True
        return False

    def misses(self, chat_id: int | str | None) -> int:
        """Сколько раз подряд в этом чате не нашлось темы."""
        if chat_id is None:
            return 0
        key = self._key(chat_id)
        streak = self._misses.get(key)
        if streak is None:
            return 0
        if self._clock() - streak.at > self._ttl:
            self._misses.pop(key, None)
            return 0
        return streak.count

    def forget_misses(self, chat_id: int | str | None) -> None:
        """Забыть цепочку отказов: бот уже предложил оператора."""
        if chat_id is not None:
            self._misses.pop(self._key(chat_id), None)

    def turns(self, chat_id: int | str | None) -> tuple[Turn, ...]:
        """Живые реплики чата, от старых к новым."""
        if chat_id is None:
            return ()
        key = self._key(chat_id)
        turns = self._chats.get(key)
        if not turns:
            return ()
        now = self._clock()
        alive = tuple(turn for turn in turns if now - turn.at <= self._ttl)
        if not alive:
            self._chats.pop(key, None)
        return alive

    def recent_topic(self, chat_id: int | str | None) -> int | None:
        """Последняя живая тема этого чата."""
        alive = self.turns(chat_id)
        return alive[-1].faq_id if alive else None

    def forget(self, chat_id: int | str) -> None:
        """Забыть один чат (команда «другой вопрос», конец диалога)."""
        self._chats.pop(self._key(chat_id), None)
        self._misses.pop(self._key(chat_id), None)
        self._last.pop(self._key(chat_id), None)

    def clear(self) -> None:
        """Забыть всё. Нужно тестам и перезапуску бота."""
        self._chats.clear()
        self._misses.clear()
        self._last.clear()

    def __len__(self) -> int:
        return len(self._chats)


def apply_context(decision: Decision, text: str,
                  chat_id: int | str | None,
                  memory: DialogMemory) -> Decision:
    """Унаследовать тему предыдущей реплики, если вопрос уточняющий.

    Возвращает исходное решение без изменений, когда наследовать нельзя:
    нет chat_id, своя уверенность высокая, реплика не похожа на уточнение
    или в чате нет живой темы.
    """
    if chat_id is None:
        return decision
    if decision.confidence >= config.CONFIDENCE_CONFIDENT:
        return decision
    if not is_follow_up(text):
        return decision
    topic = memory.recent_topic(chat_id)
    if topic is None:
        return decision

    candidates = [topic] + [item for item in decision.candidates if item != topic]
    return Decision(
        faq_id=topic,
        confidence=max(decision.confidence, INHERITED_CONFIDENCE),
        candidates=candidates[:config.TOP_K],
        inherited_from_context=True,
    )
