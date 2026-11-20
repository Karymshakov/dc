"""Константы и пути проекта.

Числа, которые задаёт ТЗ, и числа, которые выбираем мы, разделены
намеренно: первые менять нельзя, вторые настраиваются по tune-набору.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "05_support"

FAQ_PATH = DATA_DIR / "faq.json"
DEV_PATH = DATA_DIR / "questions_dev.csv"
TUNE_PATH = DATA_DIR / "tune.csv"
HOLDOUT_PATH = DATA_DIR / "holdout.csv"
DIALOGS_PATH = DATA_DIR / "dialogs_example.json"

# --- Задано ТЗ, не трогать -------------------------------------------------

LANGUAGES = ("ru", "ky")
DEFAULT_LANGUAGE = "ru"       # пустой вопрос: ru, faq_id = null
TOP_K = 3                     # «показать 3 похожих вопроса кнопками»
FAQ_SIZE = 15                 # 15 стабильных ID

# Пороги зачёта жюри. Здесь только для отчётов: мы целимся выше.
JURY_TOP1 = 0.75
JURY_TOP3 = 0.90
JURY_LANG = 0.90

# --- Выбираем мы, настраивается по tune ------------------------------------

# Ниже этого значения тема считается ненайденной: faq_id = null,
# пользователю показываются три похожих вопроса кнопками.
CONFIDENCE_THRESHOLD = 0.35

# Выше этого значения ответ выдаётся сразу и уверенно («зелёный свет»).
CONFIDENCE_CONFIDENT = 0.55

# Сколько реплик помнить в рамках одного chat_id и как долго.
CONTEXT_TURNS = 4
CONTEXT_TTL_SECONDS = 15 * 60

# Уточняющий вопрос короткий и без собственных ключевых слов.
FOLLOW_UP_MAX_WORDS = 5
