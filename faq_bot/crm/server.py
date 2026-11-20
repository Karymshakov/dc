"""Веб-сервер CRM: стандартная библиотека, JSON API, длинный опрос.

Зачем не aiohttp и не FastAPI: у проекта правило «запускается на чистом
Python». Операторов единицы, сообщений — десятки в минуту, и потоковый
сервер из stdlib с этим справляется с запасом.

Оптимизация:

* **Длинный опрос** вместо таймера: клиент висит на `/api/poll` до 25 с
  и просыпается в момент изменения. Пока ничего не происходит — ноль
  запросов, когда происходит — задержка миллисекунды.
* **Дельты**: после пробуждения клиент догружает только новые сообщения
  открытого чата (`after=<последний id>`), а не всю переписку.
* **gzip и ETag** для статики и крупных ответов: интерфейс приходит
  одним сжатым файлом и дальше берётся из кэша по 304.

Безопасность:

* пароль из `CRM_PASSWORD`; если не задан — генерируется и печатается
  при запуске, пустого пароля не бывает;
* сессия — случайный токен в cookie `HttpOnly; SameSite=Strict`;
* любой POST требует заголовок `X-CRM: 1`: чужая страница не может его
  поставить без CORS, это закрывает CSRF;
* ограничение попыток входа, CSP без внешних источников, по умолчанию
  слушаем только 127.0.0.1.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from faq_bot import config
from faq_bot.crm.hub import Hub
from faq_bot.crm.store import Store

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
POLL_TIMEOUT = 25.0
SESSION_TTL = 12 * 3600
COOKIE = "crm_session"
MAX_BODY = 64 * 1024

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self';"
       " script-src 'self'; connect-src 'self'; frame-ancestors 'none';"
       " base-uri 'none'; form-action 'self'")


def load_env() -> None:
    """Подхватить .env, не перетирая уже заданные переменные."""
    env_file = config.PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Sessions:
    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def create(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = (name, time.time() + SESSION_TTL)
        return token

    def name(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            found = self._items.get(token)
            if found is None:
                return None
            if found[1] < time.time():
                self._items.pop(token, None)
                return None
            return found[0]

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._items.pop(token or "", None)

    def throttled(self, address: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [moment for moment in self._failures.get(address, [])
                      if now - moment < 60]
            self._failures[address] = recent
            return len(recent) >= 5

    def failed(self, address: str) -> None:
        with self._lock:
            self._failures.setdefault(address, []).append(time.time())


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_static_cache: dict[str, tuple[bytes, bytes, str]] = {}


def _static(name: str) -> tuple[bytes, bytes, str] | None:
    """Файл интерфейса: (сырой, сжатый, etag). Читается один раз."""
    if name in _static_cache and not os.environ.get("CRM_DEV"):
        return _static_cache[name]
    path = (STATIC_DIR / name).resolve()
    if STATIC_DIR.resolve() not in path.parents or not path.is_file():
        return None
    raw = path.read_bytes()
    entry = (raw, gzip.compress(raw, 6), '"' + hashlib.sha1(raw).hexdigest()[:16] + '"')
    _static_cache[name] = entry
    return entry


class CrmServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], hub: Hub, password: str) -> None:
        super().__init__(address, Handler)
        self.hub = hub
        self.password = password
        self.sessions = Sessions()


ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(f"^{pattern}$"), name) for method, pattern, name in (
        ("GET", r"/api/me", "me"),
        ("POST", r"/api/login", "login"),
        ("POST", r"/api/logout", "logout"),
        ("GET", r"/api/bootstrap", "bootstrap"),
        ("GET", r"/api/poll", "poll"),
        ("GET", r"/api/conversations", "conversations"),
        ("GET", r"/api/conversations/(?P<chat>[\w\-]+)/messages", "messages"),
        ("POST", r"/api/conversations/(?P<chat>[\w\-]+)/take", "take"),
        ("POST", r"/api/conversations/(?P<chat>[\w\-]+)/release", "release"),
        ("POST", r"/api/conversations/(?P<chat>[\w\-]+)/read", "read"),
        ("POST", r"/api/conversations/(?P<chat>[\w\-]+)/send", "send"),
        ("POST", r"/api/messages/(?P<id>\d+)/tag", "tag"),
        ("GET", r"/api/learning", "learning"),
        ("POST", r"/api/learned/(?P<id>\d+)/disable", "disable"),
        ("POST", r"/api/proposals/(?P<id>\d+)/review", "review"),
        ("GET", r"/api/proposals/export", "export"),
        ("GET", r"/api/analytics", "analytics"),
        ("POST", r"/api/demo/new", "demo_new"),
        ("POST", r"/api/demo/(?P<chat>[\w\-]+)/message", "demo_message"),
        ("POST", r"/api/demo/(?P<chat>[\w\-]+)/action", "demo_action"),
    )
]

PUBLIC = {"me", "login"}


class Handler(BaseHTTPRequestHandler):
    server: CrmServer
    protocol_version = "HTTP/1.1"
    server_version = "INAI-CRM"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s %s", self.address_string(), format % args)

    # --- ответы ------------------------------------------------------------

    def _headers(self, status: int, content_type: str, length: int,
                 extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _gzip_ok(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def _json(self, payload: Any, status: int = 200,
              extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {"Cache-Control": "no-store", **(extra or {})}
        if len(body) > 1400 and self._gzip_ok():
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._headers(status, "application/json; charset=utf-8", len(body), headers)
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    # --- маршрутизация -----------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        if method == "GET" and not url.path.startswith("/api/"):
            self._serve_static(url.path)
            return
        for route_method, pattern, name in ROUTES:
            match = pattern.match(url.path)
            if match and route_method == method:
                break
        else:
            self._error(404, "нет такого адреса")
            return
        try:
            operator = self._operator()
            if name not in PUBLIC and operator is None:
                raise ApiError(401, "нужен вход")
            if method == "POST" and self.headers.get("X-CRM") != "1":
                raise ApiError(403, "запрос без заголовка X-CRM")
            body = self._body() if method == "POST" else {}
            query = {key: values[-1] for key, values in parse_qs(url.query).items()}
            getattr(self, f"api_{name}")(operator, body=body, query=query,
                                         **match.groupdict())
        except ApiError as error:
            self._error(error.status, error.message)
        except KeyError:
            self._error(404, "не найдено")
        except (ValueError, PermissionError) as error:
            self._error(400, str(error))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("CRM: ошибка в %s %s", method, url.path)
            self._error(500, "внутренняя ошибка")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "слишком большой запрос")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, "тело запроса не JSON") from error
        if not isinstance(data, dict):
            raise ApiError(400, "ожидался объект JSON")
        return data

    def _token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else None

    def _operator(self) -> str | None:
        return self.server.sessions.name(self._token())

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        entry = _static(name)
        if entry is None:
            self._error(404, "нет такого файла")
            return
        raw, packed, etag = entry
        content_type = STATIC_TYPES.get(Path(name).suffix, "application/octet-stream")
        cache = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            self._headers(304, content_type, 0, cache)
            return
        body = raw
        if self._gzip_ok():
            body = packed
            cache["Content-Encoding"] = "gzip"
        self._headers(200, content_type, len(body), cache)
        self.wfile.write(body)

    # --- API ---------------------------------------------------------------

    @property
    def hub(self) -> Hub:
        return self.server.hub

    def api_me(self, operator, **_):
        self._json({"name": operator})

    def api_login(self, operator, body, **_):
        address = self.client_address[0]
        if self.server.sessions.throttled(address):
            raise ApiError(429, "слишком много попыток, подождите минуту")
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:40]
        password = str(body.get("password") or "")
        if not name:
            raise ApiError(400, "укажите имя оператора")
        if not hmac.compare_digest(password.encode(), self.server.password.encode()):
            self.server.sessions.failed(address)
            raise ApiError(403, "неверный пароль")
        token = self.server.sessions.create(name)
        cookie = (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict;"
                  f" Max-Age={SESSION_TTL}")
        self._json({"name": name}, extra={"Set-Cookie": cookie})

    def api_logout(self, operator, **_):
        self.server.sessions.drop(self._token())
        self._json({"ok": True}, extra={
            "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

    def api_bootstrap(self, operator, **_):
        from faq_bot import knowledge

        faq = [{"id": entry.id, "question_ru": entry.question_ru,
                "question_ky": entry.question_ky, "answer_ru": entry.answer_ru,
                "answer_ky": entry.answer_ky} for entry in knowledge.default_faq()]
        self._json({
            "me": operator,
            "version": self.hub.store.feed.version,
            "faq": faq,
            "threshold": config.CONFIDENCE_THRESHOLD,
            "conversations": self.hub.store.conversations(),
            "now": time.time(),
        })

    def api_poll(self, operator, query, **_):
        since = int(query.get("v") or 0)
        version, changed = self.hub.store.feed.wait(since, POLL_TIMEOUT)
        self._json({"version": version, "changed": changed, "now": time.time()})

    def api_conversations(self, operator, **_):
        self._json({"conversations": self.hub.store.conversations(),
                    "now": time.time()})

    def api_messages(self, operator, chat, query, **_):
        if self.hub.store.conversation(chat) is None:
            raise KeyError(chat)
        before = query.get("before")
        rows = self.hub.store.messages(
            chat, after=int(query.get("after") or 0),
            before=int(before) if before else None,
            limit=min(200, int(query.get("limit") or 80)))
        for row in rows:
            row["buttons"] = json.loads(row["buttons"]) if row["buttons"] else None
        self._json({"messages": rows,
                    "conversation": self.hub.store.conversation(chat)})

    def api_take(self, operator, chat, **_):
        self._json({"conversation": self.hub.take(chat, operator)})

    def api_release(self, operator, chat, **_):
        self._json({"conversation": self.hub.release(chat, operator)})

    def api_read(self, operator, chat, **_):
        self.hub.mark_read(chat)
        self._json({"ok": True})

    def api_send(self, operator, chat, body, **_):
        faq_id = body.get("faq_id")
        result = self.hub.send(chat, operator, str(body.get("text") or ""),
                               propose=bool(body.get("propose")),
                               faq_id=int(faq_id) if faq_id else None)
        self._json(result)

    def api_tag(self, operator, id, body, **_):  # noqa: A002
        faq_id = body.get("faq_id")
        self._json(self.hub.tag(int(id), int(faq_id) if faq_id else None, operator))

    def api_learning(self, operator, **_):
        self._json(self.hub.learning_state())

    def api_disable(self, operator, id, **_):  # noqa: A002
        self.hub.disable_learned(int(id))
        self._json({"ok": True})

    def api_review(self, operator, id, body, **_):  # noqa: A002
        self.hub.review(int(id), str(body.get("status") or ""), operator)
        self._json({"ok": True})

    def api_export(self, operator, **_):
        payload = {
            "_doc": "Выгрузка CRM. learned — фразы для слоя синонимов "
                    "faq_bot/knowledge.py (KEYWORDS); proposals — одобренные "
                    "старшим правки ответов. faq.json автоматически не меняется.",
            "learned": [{"faq_id": row["faq_id"], "phrase": row["phrase"],
                         "operator": row["operator"]}
                        for row in self.hub.store.learned()],
            "proposals": self.hub.store.proposals("approved"),
        }
        self._json(payload, extra={
            "Content-Disposition": 'attachment; filename="crm_export.json"'})

    def api_analytics(self, operator, **_):
        self._json(self.hub.analytics())

    def api_demo_new(self, operator, **_):
        self._json({"conversation": self.hub.new_demo_chat()})

    def api_demo_message(self, operator, chat, body, **_):
        self.hub.demo_message(chat, str(body.get("text") or ""))
        self._json({"ok": True})

    def api_demo_action(self, operator, chat, body, **_):
        self.hub.demo_action(chat, str(body.get("action") or ""))
        self._json({"ok": True})


def build_server(host: str | None = None, port: int | None = None,
                 hub: Hub | None = None, password: str | None = None) -> CrmServer:
    load_env()
    host = host or os.environ.get("CRM_HOST", "127.0.0.1")
    # port=0 — законный «любой свободный порт», поэтому проверка на None.
    port = int(port if port is not None else (os.environ.get("CRM_PORT") or 8765))
    password = password or os.environ.get("CRM_PASSWORD", "").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(9)
    if hub is None:
        retention = float(os.environ.get("CRM_RETENTION_DAYS") or 30)
        db_path = os.environ.get("CRM_DB") or None
        hub = Hub(Store(db_path), retention_days=retention)
    server = CrmServer((host, port), hub, password)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"CRM операторов: http://{shown}:{port}")
    if generated:
        print(f"  пароль (CRM_PASSWORD не задан, создан на этот запуск): {password}")
    return server


def start_in_background(**kwargs: Any) -> CrmServer:
    """Поднять CRM в фоновом потоке рядом с ботом."""
    server = build_server(**kwargs)
    thread = threading.Thread(target=server.serve_forever, name="crm-http",
                              daemon=True)
    thread.start()
    return server


def serve(host: str | None = None, port: int | None = None) -> int:
    """Только CRM, без Telegram: демо-клиент и разметка работают полностью."""
    from faq_bot import bot

    server = build_server(host, port)
    bot.attach_crm(server.hub)
    print("Режим без Telegram: абонента изображает кнопка «Демо-клиент».")
    print("Остановить — Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
    finally:
        server.server_close()
    return 0
