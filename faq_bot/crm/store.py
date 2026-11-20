"""Хранилище CRM на SQLite: только стандартная библиотека.

Одно соединение на процесс под замком. Писателей два — поток бота и
потоки веб-сервера, — а нагрузка на поддержку измеряется сообщениями в
секунду, не тысячами. Режим WAL позволяет читать во время записи.

Лента изменений (`Feed`) живёт в памяти: веб-клиенты держат длинный
опрос и просыпаются, как только версия выросла. Так интерфейс получает
новое сообщение сразу, не опрашивая сервер каждую секунду.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from faq_bot import config

DEFAULT_DB_PATH = config.PROJECT_ROOT / "data" / "crm.sqlite3"

#: Статусы диалога.
BOT = "bot"            # ведёт бот
WAITING = "waiting"    # абонент попросил человека, оператор ещё не взял
OPERATOR = "operator"  # ведёт оператор, бот молчит
STATUSES = (BOT, WAITING, OPERATOR)

#: Направления сообщений.
IN = "in"      # от абонента
OUT_BOT = "bot"
OUT_OP = "op"
SYSTEM = "sys"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_id        TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    username       TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'telegram',
    lang           TEXT NOT NULL DEFAULT 'ru',
    status         TEXT NOT NULL DEFAULT 'bot',
    operator       TEXT,
    reason         TEXT,
    waiting_since  REAL,
    topic          INTEGER,
    last_text      TEXT NOT NULL DEFAULT '',
    last_dir       TEXT NOT NULL DEFAULT '',
    unread         INTEGER NOT NULL DEFAULT 0,
    bot_misses     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conv_updated ON conversations(updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    dir         TEXT NOT NULL,
    text        TEXT NOT NULL,
    author      TEXT,
    faq_id      INTEGER,
    confidence  REAL,
    unanswered  INTEGER NOT NULL DEFAULT 0,
    tag         TEXT,
    buttons     TEXT,
    wait        REAL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS msg_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS msg_unanswered ON messages(unanswered, tag);

CREATE TABLE IF NOT EXISTS learned (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase      TEXT NOT NULL,
    faq_id      INTEGER NOT NULL,
    operator    TEXT NOT NULL,
    message_id  INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    report      TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operators (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    login          TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'operator',
    password_hash  TEXT NOT NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    last_login     REAL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    lang        TEXT NOT NULL DEFAULT 'ru',
    faq_id      INTEGER,
    operator    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    reviewer    TEXT,
    created_at  REAL NOT NULL
);
"""


class Feed:
    """Версия данных и ожидание её роста для длинного опроса."""

    def __init__(self, keep: int = 512) -> None:
        self._version = 0
        self._log: deque[tuple[int, str]] = deque(maxlen=keep)
        self._cond = threading.Condition()

    @property
    def version(self) -> int:
        return self._version

    def bump(self, chat_id: str | None = None, kind: str = "chat") -> int:
        with self._cond:
            self._version += 1
            self._log.append((self._version, f"{kind}:{chat_id or ''}"))
            self._cond.notify_all()
            return self._version

    def wait(self, since: int, timeout: float) -> tuple[int, list[str]]:
        """Дождаться версии новее `since` или таймаута.

        Возвращает текущую версию и список изменившихся объектов. Если
        клиент отстал больше, чем помнит журнал, получает `["*"]`:
        перечитать всё.
        """
        with self._cond:
            if since > self._version:      # сервер перезапущен
                return self._version, ["*"]
            if self._version <= since:
                self._cond.wait(timeout)
            changed = [key for version, key in self._log if version > since]
            oldest = self._log[0][0] if self._log else self._version + 1
            if since and since + 1 < oldest and self._version > since:
                changed = ["*"]
            return self._version, sorted(set(changed))


def _row(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    return {column[0]: value for column, value in zip(cursor.description, row)}


class Store:
    """Доступ к базе CRM. Все методы потокобезопасны."""

    def __init__(self, path: Path | str | None = None,
                 clock=time.time) -> None:
        self.path = str(path) if path is not None else str(DEFAULT_DB_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = _row
        self._lock = threading.RLock()
        self.clock = clock
        self.feed = Feed()
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    # --- служебное ---------------------------------------------------------

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchone()

    def _run(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cursor = self._db.execute(sql, tuple(params))
            return int(cursor.lastrowid or 0)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- диалоги -----------------------------------------------------------

    def conversation(self, chat_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM conversations WHERE chat_id = ?",
                         (str(chat_id),))

    def ensure(self, chat_id: str, title: str = "", username: str = "",
               source: str = "telegram", lang: str | None = None) -> dict[str, Any]:
        """Диалог по chat_id; создаётся при первом сообщении."""
        chat_id = str(chat_id)
        now = self.clock()
        with self._lock:
            found = self.conversation(chat_id)
            if found is None:
                self._run(
                    "INSERT INTO conversations (chat_id, title, username, source,"
                    " lang, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (chat_id, title, username, source, lang or config.DEFAULT_LANGUAGE,
                     now, now))
            else:
                updates: dict[str, Any] = {}
                if title and title != found["title"]:
                    updates["title"] = title
                if username and username != found["username"]:
                    updates["username"] = username
                if lang and lang != found["lang"]:
                    updates["lang"] = lang
                if updates:
                    self.update(chat_id, **updates)
            return self.conversation(chat_id)  # type: ignore[return-value]

    _UPDATABLE = frozenset((
        "title", "username", "lang", "status", "operator", "reason",
        "waiting_since", "topic", "last_text", "last_dir", "unread",
        "bot_misses", "updated_at",
    ))

    def update(self, chat_id: str, **fields: Any) -> None:
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValueError(f"неизвестные поля: {sorted(unknown)}")
        if not fields:
            return
        columns = ", ".join(f"{name} = ?" for name in fields)
        self._run(f"UPDATE conversations SET {columns} WHERE chat_id = ?",
                  (*fields.values(), str(chat_id)))

    def conversations(self, limit: int = 300) -> list[dict[str, Any]]:
        """Все диалоги, свежие первыми; группировку делает клиент."""
        return self._all(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (int(limit),))

    # --- сообщения ---------------------------------------------------------

    def add_message(self, chat_id: str, direction: str, text: str,
                    author: str | None = None, faq_id: int | None = None,
                    confidence: float | None = None, unanswered: bool = False,
                    buttons: list[list[str]] | None = None,
                    tag: str | None = None, wait: float | None = None) -> int:
        now = self.clock()
        return self._run(
            "INSERT INTO messages (chat_id, dir, text, author, faq_id, confidence,"
            " unanswered, tag, buttons, wait, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(chat_id), direction, text, author, faq_id, confidence,
             1 if unanswered else 0, tag,
             json.dumps(buttons, ensure_ascii=False) if buttons else None,
             wait, now))

    def edit_message(self, message_id: int, **fields: Any) -> None:
        allowed = {"text", "faq_id", "confidence", "unanswered", "tag", "buttons"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"неизвестные поля: {sorted(unknown)}")
        if "buttons" in fields and fields["buttons"] is not None:
            fields["buttons"] = json.dumps(fields["buttons"], ensure_ascii=False)
        if "unanswered" in fields:
            fields["unanswered"] = 1 if fields["unanswered"] else 0
        columns = ", ".join(f"{name} = ?" for name in fields)
        self._run(f"UPDATE messages SET {columns} WHERE id = ?",
                  (*fields.values(), int(message_id)))

    def message(self, message_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM messages WHERE id = ?", (int(message_id),))

    def last_message(self, chat_id: str, direction: str | None = None
                     ) -> dict[str, Any] | None:
        if direction is None:
            return self._one("SELECT * FROM messages WHERE chat_id = ?"
                             " ORDER BY id DESC LIMIT 1", (str(chat_id),))
        return self._one("SELECT * FROM messages WHERE chat_id = ? AND dir = ?"
                         " ORDER BY id DESC LIMIT 1", (str(chat_id), direction))

    def messages(self, chat_id: str, after: int = 0, before: int | None = None,
                 limit: int = 80) -> list[dict[str, Any]]:
        """Сообщения диалога по возрастанию id.

        `after` — догрузка новых, `before` — подгрузка старых порцией.
        """
        if before is not None:
            rows = self._all(
                "SELECT * FROM messages WHERE chat_id = ? AND id < ?"
                " ORDER BY id DESC LIMIT ?", (str(chat_id), int(before), int(limit)))
            return list(reversed(rows))
        if after:
            return self._all(
                "SELECT * FROM messages WHERE chat_id = ? AND id > ?"
                " ORDER BY id LIMIT ?", (str(chat_id), int(after), int(limit)))
        rows = self._all(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (str(chat_id), int(limit)))
        return list(reversed(rows))

    def unanswered(self, limit: int = 100) -> list[dict[str, Any]]:
        """Реплики, на которые бот не нашёл тему и которые ещё не размечены."""
        return self._all(
            "SELECT m.*, c.title, c.lang AS chat_lang FROM messages m"
            " JOIN conversations c ON c.chat_id = m.chat_id"
            " WHERE m.unanswered = 1 AND m.tag IS NULL"
            " ORDER BY m.id DESC LIMIT ?", (int(limit),))

    # --- обучение ----------------------------------------------------------

    def add_learned(self, phrase: str, faq_id: int, operator: str,
                    message_id: int | None, report: dict[str, Any]) -> int:
        return self._run(
            "INSERT INTO learned (phrase, faq_id, operator, message_id, report,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (phrase, int(faq_id), operator, message_id,
             json.dumps(report, ensure_ascii=False), self.clock()))

    def learned(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM learned"
        if active_only:
            sql += " WHERE active = 1"
        return self._all(sql + " ORDER BY id DESC")

    def set_learned_active(self, learned_id: int, active: bool) -> None:
        self._run("UPDATE learned SET active = ? WHERE id = ?",
                  (1 if active else 0, int(learned_id)))

    # --- предложения в базу ------------------------------------------------

    def add_proposal(self, question: str, answer: str, lang: str,
                     faq_id: int | None, operator: str,
                     chat_id: str | None) -> int:
        return self._run(
            "INSERT INTO proposals (chat_id, question, answer, lang, faq_id,"
            " operator, created_at) VALUES (?,?,?,?,?,?,?)",
            (chat_id, question, answer, lang, faq_id, operator, self.clock()))

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM proposals WHERE status = ?"
                             " ORDER BY id DESC", (status,))
        return self._all("SELECT * FROM proposals ORDER BY id DESC")

    def review_proposal(self, proposal_id: int, status: str, reviewer: str) -> None:
        self._run("UPDATE proposals SET status = ?, reviewer = ? WHERE id = ?",
                  (status, reviewer, int(proposal_id)))

    # --- учётные записи ----------------------------------------------------

    def add_operator(self, login: str, name: str, role: str,
                     password_hash: str) -> int:
        return self._run(
            "INSERT INTO operators (login, name, role, password_hash, created_at)"
            " VALUES (?,?,?,?,?)", (login, name, role, password_hash, self.clock()))

    def operator(self, operator_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM operators WHERE id = ?", (int(operator_id),))

    def operator_by_login(self, login: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM operators WHERE login = ?", (login,))

    def operators(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM operators ORDER BY role, login")

    def update_operator(self, operator_id: int, **fields: Any) -> None:
        allowed = {"name", "role", "active", "password_hash", "last_login"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"неизвестные поля: {sorted(unknown)}")
        columns = ", ".join(f"{name} = ?" for name in fields)
        self._run(f"UPDATE operators SET {columns} WHERE id = ?",
                  (*fields.values(), int(operator_id)))

    # --- аналитика и уборка ------------------------------------------------

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self._one(sql, params)
        return next(iter(row.values())) if row else None

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self._all(sql, params)

    def purge(self, older_than_days: float) -> int:
        """Удалить переписку старше срока хранения. Возвращает число строк."""
        if older_than_days <= 0:
            return 0
        border = self.clock() - older_than_days * 86400
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM messages WHERE created_at < ?", (border,))
            removed = cursor.rowcount
            self._db.execute(
                "DELETE FROM conversations WHERE updated_at < ? AND chat_id NOT IN"
                " (SELECT DISTINCT chat_id FROM messages)", (border,))
            return int(removed or 0)
