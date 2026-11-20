"""Голосовое сообщение абонента в текст. Бонусный пункт ТЗ.

Подход и защитные механизмы перенесены из рабочего ассистента AXON
(backend/apps/leads/services/speech.py), где они обкатаны на живых гостях.

Почему Gemini, а не офлайн-модель. Для кыргызского языка офлайн-распознавания
практически нет: открытая AkylAI-STT-small существует, но её никто не
хостит, а поднимать её самим означает torch и около двух гигабайт
зависимостей. У проекта принцип «answer() работает на чистой стандартной
библиотеке», и ломать его ради бонуса нельзя. Gemini понимает кыргызский,
вызывается обычным HTTP-запросом и нужен только боту.

ЭКОНОМИЯ — ГЛАВНОЕ ТРЕБОВАНИЕ ЭТОГО МОДУЛЯ.

Ключ оплачивается человеком, поэтому модуль устроен так, чтобы потратить
как можно меньше:

* Gemini вызывается ТОЛЬКО для превращения звука в текст. Поиск ответа
  остаётся на нашем бесплатном правиловом движке. Текстовые сообщения
  не стоят ничего вообще.
* Аудио тарифицируется по секундам, а не по байтам: примерно 25 токенов
  на секунду записи против 6 токенов на весь текст запроса. Поэтому
  модуль тратит основные усилия на измерение и ограничение длительности.
* Записи длиннее минуты не распознаются вовсе — абонент получает просьбу
  сказать короче. Минута речи это уже абзац.
* Есть потолок на чат, на час и на сутки. Тот, кто решит скормить боту
  подкаст, упрётся в лимит раньше, чем в кошелёк владельца ключа.
* Модель по умолчанию — самая дешёвая из линейки flash-lite.

Функция никогда не бросает исключение: неудачное распознавание обязано
оставить диалог там, где он был, а не уронить ответ.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")
DEFAULT_MODEL = "gemini-flash-lite-latest"
TIMEOUT_SECONDS = 60

# --- Пределы. Все до единого про чужие деньги ------------------------------

# Дольше этого не распознаём. В AXON стоит 120 секунд; здесь вдвое меньше,
# потому что вопрос в справочную короче, чем рассказ о планах на отпуск.
MAX_AUDIO_SECONDS = 60.0

# Меньше этого — не речь, а случайное нажатие на микрофон.
MIN_AUDIO_BYTES = 1024

# Предел инлайн-передачи: больше этого Gemini требует отдельной загрузки,
# которую мы сознательно не делаем.
MAX_INLINE_BYTES = 4 * 1024 * 1024

# Оценка размера опуса из Telegram, когда длительность не пришла в метаданных.
ASSUMED_BYTES_PER_SECOND = 2000

# Бюджеты в секундах распознанного звука.
BUDGET_PER_CHAT_HOUR = 180.0
BUDGET_TOTAL_HOUR = 900.0
BUDGET_TOTAL_DAY = 3600.0

MIME_BY_SUFFIX = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
}

# Статусы результата.
OK = "ok"
NOTHING = "nothing"
TOO_LONG = "too_long"
RATE_LIMITED = "rate_limited"
NO_KEY = "no_key"
FAILED = "failed"


@dataclass(frozen=True)
class Transcript:
    """Что услышано, и почему не услышано, когда не услышано."""

    status: str
    text: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == OK and bool(self.text.strip())


class Budget:
    """Учёт потраченных секунд по чатам и по всему боту.

    Хранится в памяти процесса: перезапуск обнуляет. Для хакатона этого
    достаточно, а база ради счётчика — лишняя зависимость.
    """

    def __init__(self) -> None:
        self._events: list[tuple[float, str, float]] = []

    def _spent(self, since: float, chat_id: str | None = None) -> float:
        return sum(seconds for moment, chat, seconds in self._events
                   if moment >= since and (chat_id is None or chat == chat_id))

    def allows(self, chat_id: str | None, seconds: float) -> tuple[bool, str]:
        """Можно ли потратить столько секунд прямо сейчас."""
        now = time.time()
        hour, day = now - 3600, now - 86400

        if chat_id is not None:
            used = self._spent(hour, str(chat_id))
            if used + seconds > BUDGET_PER_CHAT_HOUR:
                return False, f"чат израсходовал {used:.0f} с за час"
        if self._spent(hour) + seconds > BUDGET_TOTAL_HOUR:
            return False, "общий часовой лимит исчерпан"
        if self._spent(day) + seconds > BUDGET_TOTAL_DAY:
            return False, "общий суточный лимит исчерпан"
        return True, ""

    def record(self, chat_id: str | None, seconds: float) -> None:
        now = time.time()
        self._events = [item for item in self._events if item[0] >= now - 86400]
        self._events.append((now, str(chat_id), seconds))

    def report(self) -> dict[str, float]:
        """Сколько потрачено — для /stats и для совести."""
        now = time.time()
        return {
            "за час, с": round(self._spent(now - 3600), 1),
            "за сутки, с": round(self._spent(now - 86400), 1),
            "лимит в сутки, с": BUDGET_TOTAL_DAY,
        }


BUDGET = Budget()


def api_key() -> str:
    """Ключ из окружения или из .env рядом с проектом."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "GEMINI_API_KEY":
                return value.strip()
    return ""


def guess_mime(path: str | Path) -> str:
    return MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "audio/ogg")


def estimate_seconds(path: Path, declared: float = 0.0) -> float:
    """Длительность записи: из метаданных Telegram или по размеру файла."""
    if declared and declared > 0:
        return float(declared)
    try:
        return path.stat().st_size / ASSUMED_BYTES_PER_SECOND
    except OSError:
        return 0.0


def build_prompt(expected_languages: tuple[str, ...] = ("кыргызском", "русском")) -> str:
    """Указание модели. Коротко: каждый лишний токен оплачен.

    Просим только расшифровку, без пересказа и без ответа на вопрос —
    отвечать будет наш собственный движок по базе faq.json.
    """
    languages = " или ".join(expected_languages)
    return (f"Запиши текстом, что сказано в этой записи. Речь на {languages} "
            "языке. Верни только сам текст, без пояснений и без перевода. "
            "Если речи нет — верни пустую строку.")


def transcribe(file_path: str | Path, *, chat_id: str | None = None,
               declared_seconds: float = 0.0,
               model: str = DEFAULT_MODEL) -> Transcript:
    """Текст голосового сообщения. Никогда не бросает исключение.

    Порядок проверок выбран от дешёвого к дорогому: всё, что можно
    отклонить без обращения к сети, отклоняется до него.
    """
    path = Path(file_path)
    if not path.exists():
        return Transcript(NOTHING)

    try:
        size = path.stat().st_size
    except OSError:
        return Transcript(NOTHING)

    if size < MIN_AUDIO_BYTES:
        return Transcript(NOTHING)
    if size > MAX_INLINE_BYTES:
        logger.info("Голосовое отклонено: %s байт больше инлайн-предела", size)
        return Transcript(TOO_LONG, seconds=size / ASSUMED_BYTES_PER_SECOND)

    seconds = estimate_seconds(path, declared_seconds)
    if seconds > MAX_AUDIO_SECONDS:
        logger.info("Голосовое отклонено: %.0f с больше предела %.0f",
                    seconds, MAX_AUDIO_SECONDS)
        return Transcript(TOO_LONG, seconds=seconds)

    allowed, reason = BUDGET.allows(chat_id, seconds)
    if not allowed:
        logger.warning("Распознавание отклонено для чата %s: %s", chat_id, reason)
        return Transcript(RATE_LIMITED, seconds=seconds)

    key = api_key()
    if not key:
        logger.warning("Нет GEMINI_API_KEY — голосовые не распознаются")
        return Transcript(NO_KEY, seconds=seconds)

    body = {
        "contents": [{
            "parts": [
                {"text": build_prompt()},
                {"inline_data": {
                    "mime_type": guess_mime(path),
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }},
            ],
        }],
        # Расшифровка короткой реплики не бывает длинной. Потолок защищает
        # от попытки модели разговориться за наш счёт.
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 256},
    }

    request = urllib.request.Request(
        ENDPOINT.format(model=model) + f"?key={key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        logger.warning("Gemini ответил %s на распознавание", error.code)
        return Transcript(FAILED, seconds=seconds)
    except Exception as error:                      # сеть, таймаут, разбор
        logger.warning("Распознавание не удалось: %s", error)
        return Transcript(FAILED, seconds=seconds)

    text = extract_text(payload)
    BUDGET.record(chat_id, seconds)
    if not text:
        return Transcript(NOTHING, seconds=seconds)

    logger.info("Распознано %.1f с речи, %d знаков", seconds, len(text))
    return Transcript(OK, text=text, seconds=seconds)


def extract_text(payload: dict) -> str:
    """Достать текст из ответа Gemini, не предполагая его формы."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    pieces = [part.get("text", "") for part in parts if isinstance(part, dict)]
    return " ".join(piece.strip() for piece in pieces if piece).strip()
