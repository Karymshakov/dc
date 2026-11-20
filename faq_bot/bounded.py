"""Хранилища состояния с потолком по числу чатов.

Зачем
-----

Бот держит состояние по каждому собеседнику: выбранный язык, счётчики
ограничителя, открытое окно дозревания, последняя реплика для оператора.
Естественный способ это записать — `dict[chat_id, …]`, и он же неверный:
запись появляется на первом сообщении и не исчезает никогда. Процесс
бота живёт сутками, число разных собеседников только растёт.

Для демонстрации на двадцати чатах это ничего не значит. Для оператора
связи это, во-первых, утечка памяти, а во-вторых дешёвая атака: достаточно
написать боту с большого числа аккаунтов, и процесс съест всю память,
не нарушив ни одного ограничения на частоту — ограничитель сам хранится
в таком же словаре.

Решение
-------

Потолок и вытеснение самого старого. Состояние чата — не данные, а кеш:
потеря записи означает, что собеседник получит ответ как новый (язык
определится заново, окно дозревания закроется), а не ошибку. Поэтому
вытеснять можно молча.

Потолок общий и щедрый: :data:`DEFAULT_LIMIT` чатов на структуру. Этого
заведомо хватает на любое число одновременных разговоров, при котором
однопроцессный бот вообще работоспособен.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, MutableMapping, MutableSet
from typing import TypeVar

#: Сколько чатов помним в одной структуре. Порядок величины выбран от
#: обратного: однопроцессный бот на одном ядре обслуживает порядка
#: сотен сообщений в секунду (см. README, раздел про нагрузку), а
#: активными одновременно бывают единицы процентов собеседников.
DEFAULT_LIMIT = 10_000

K = TypeVar("K")
V = TypeVar("V")


class BoundedDict(MutableMapping[K, V]):
    """Словарь, который выбрасывает самую старую запись при переполнении.

    Ведёт себя как обычный `dict` во всём, кроме одного: при добавлении
    записи сверх потолка молча удаляется та, что была добавлена раньше
    всех. Обращение к записи её не освежает — вытеснение идёт по
    времени записи, не по времени чтения. Так проще и предсказуемее, а
    для состояния разговора разницы нет: активный чат пишет в своё
    состояние на каждой реплике.
    """

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        if limit < 1:
            raise ValueError("потолок меньше одной записи лишён смысла")
        self._limit = limit
        self._items: OrderedDict[K, V] = OrderedDict()

    def __getitem__(self, key: K) -> V:
        return self._items[key]

    def __setitem__(self, key: K, value: V) -> None:
        if key in self._items:
            self._items[key] = value
            return
        self._items[key] = value
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def __delitem__(self, key: K) -> None:
        del self._items[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:                        # pragma: no cover
        return f"BoundedDict({len(self._items)}/{self._limit})"


class BoundedSet(MutableSet[K]):
    """Множество с тем же потолком и тем же правилом вытеснения."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        if limit < 1:
            raise ValueError("потолок меньше одной записи лишён смысла")
        self._limit = limit
        self._items: OrderedDict[K, None] = OrderedDict()

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def add(self, key: K) -> None:
        if key in self._items:
            return
        self._items[key] = None
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def discard(self, key: K) -> None:
        self._items.pop(key, None)   # type: ignore[arg-type]

    def clear(self) -> None:
        self._items.clear()

    def __repr__(self) -> str:                        # pragma: no cover
        return f"BoundedSet({len(self._items)}/{self._limit})"
