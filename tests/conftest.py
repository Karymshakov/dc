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
