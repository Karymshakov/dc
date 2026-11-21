"""Общая настройка тестов: корень проекта в sys.path, чистое состояние.

Тесты запускаются из корня репозитория:

    python -m pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def clean_state():
    """Состояние процесса не переносится между тестами.

    Кроме контекста диалогов и статистики сбрасывается состояние слоя
    бота: выбранный язык, счётчики троттлинга и буферы склейки. Иначе
    тесты зависели бы от порядка запуска — чат, который уже получил
    пять ответов, в следующем тесте оказался бы заглушен.
    """
    from faq_bot import answer as answer_module

    def reset() -> None:
        answer_module.MEMORY.clear()
        answer_module.STATS.reset()
        try:
            from faq_bot import bot as bot_module
        except ImportError:               # pragma: no cover
            return
        bot_module.reset_runtime_state()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def model_is_off_unless_a_test_asks():
    """Ни один тест не обращается к настоящей модели без спроса.

    Переформулировка вызывается из обработчика текста, поэтому при
    `REPHRASE_ENABLED=1` в `.env` разработчика **любой** тест переписки
    уходил бы в сеть. Это не теория: первый же прогон после подключения
    дал пять падений, а числа менялись от запуска к запуску — тесты
    сравнивали дословный текст базы с тем, что в этот раз сочинила
    модель. Платил бы при этом владелец ключа.

    Поэтому модуль здесь выключен, а транспорт заменён на запрещающий:
    тест, которому модель нужна, включает её сам и подставляет свой
    ответ. Прежние значения возвращаются после теста — иначе
    выключатель остался бы сломанным для остального прогона.

    То же самое со вторым мнением (`faq_bot.router`): оно вызывается на
    каждом отказе и ходит в сеть тем же транспортом, поэтому при
    `LLM_ROUTER_ENABLED=1` в `.env` выключается здесь же.
    """
    from faq_bot import rephrase, router

    saved_enabled = rephrase.ENABLED
    saved_transport = rephrase.transport
    saved_router = router.ENABLED
    router.ENABLED = False

    def forbidden(*args: object, **kwargs: object) -> dict:
        raise AssertionError(
            "тест обратился к настоящей модели: нужен подменный транспорт")

    rephrase.ENABLED = False
    rephrase.transport = forbidden
    rephrase.reset()
    yield
    rephrase.ENABLED = saved_enabled
    rephrase.transport = saved_transport
    router.ENABLED = saved_router
    rephrase.reset()
