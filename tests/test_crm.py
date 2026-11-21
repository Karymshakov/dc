"""CRM операторов: перехват, очередь, обучение с проверкой, HTTP API."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

from faq_bot import answer, bot, knowledge
from faq_bot.crm import store as st
from faq_bot.crm.hub import Hub
from faq_bot.crm.server import build_server


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hub(clock):
    created = Hub(st.Store(":memory:", clock=clock), retention_days=0)
    bot.attach_crm(created)
    yield created
    bot.attach_crm(None)
    created.learner.apply({})


def demo(hub: Hub, name: str = "Айгерим") -> str:
    chat = hub.new_demo_chat()["chat_id"]
    hub.store.update(chat, title=name)
    return chat


# --- перехват и очередь ---------------------------------------------------

def test_бот_отвечает_и_пишет_тему_в_журнал(hub):
    chat = demo(hub)
    hub.demo_message(chat, "Как проверить баланс?")
    incoming, reply = hub.store.messages(chat)
    assert incoming["dir"] == st.IN and incoming["faq_id"] == 1
    assert reply["dir"] == st.OUT_BOT and reply["faq_id"] == 1
    assert "*100#" in reply["text"]


def test_непонятая_реплика_попадает_в_очередь_обучения(hub):
    chat = demo(hub)
    hub.demo_message(chat, "Курс доллара на сегодня")
    queue = hub.store.unanswered()
    assert [item["text"] for item in queue] == ["Курс доллара на сегодня"]


def test_приветствие_не_засоряет_очередь(hub):
    chat = demo(hub)
    hub.demo_message(chat, "салам")
    assert hub.store.unanswered() == []


def test_слово_оператор_ставит_диалог_в_очередь(hub, clock):
    chat = demo(hub)
    hub.demo_message(chat, "оператор")
    conversation = hub.store.conversation(chat)
    assert conversation["status"] == st.WAITING
    assert conversation["waiting_since"] == clock.now
    assert conversation["reason"] == "direct_request"


def test_после_передачи_бот_молчит(hub):
    chat = demo(hub)
    hub.demo_message(chat, "оператор")
    before = len(hub.store.messages(chat))
    hub.demo_message(chat, "Как проверить баланс?")
    after = hub.store.messages(chat)
    assert len(after) == before + 1
    assert after[-1]["dir"] == st.IN


def test_перехват_и_возврат_боту(hub):
    chat = demo(hub)
    hub.demo_message(chat, "Как проверить баланс?")
    hub.take(chat, "Айгуль")
    assert hub.store.conversation(chat)["status"] == st.OPERATOR
    hub.demo_message(chat, "а ещё вопрос")
    assert hub.store.messages(chat)[-1]["dir"] == st.IN       # бот не ответил

    hub.release(chat, "Айгуль")
    conversation = hub.store.conversation(chat)
    assert conversation["status"] == st.BOT
    assert conversation["waiting_since"] is None
    hub.demo_message(chat, "Как проверить баланс?")
    assert hub.store.messages(chat)[-1]["dir"] == st.OUT_BOT


def test_ответ_оператора_снимает_ожидание_и_меряет_время(hub, clock):
    chat = demo(hub)
    hub.demo_message(chat, "оператор")
    clock.now += 90
    result = hub.send(chat, "Айгуль", "Здравствуйте, слушаю вас")
    assert result["delivered"]
    conversation = hub.store.conversation(chat)
    assert conversation["waiting_since"] is None
    assert conversation["operator"] == "Айгуль"
    assert hub.store.message(result["message_id"])["wait"] == pytest.approx(90)


def test_очередь_держит_старое_ожидание(hub, clock):
    """Новая реплика не сдвигает время ожидания: старые не тонут."""
    chat = demo(hub)
    hub.demo_message(chat, "оператор")
    started = hub.store.conversation(chat)["waiting_since"]
    clock.now += 600
    hub.demo_message(chat, "ну где вы?")
    assert hub.store.conversation(chat)["waiting_since"] == started


def test_ответ_оператора_в_базу_идёт_на_проверку_а_не_в_бота(hub):
    chat = demo(hub)
    hub.demo_message(chat, "оператор")
    hub.send(chat, "Айгуль", "Роуминг стоит 1 сом", propose=True)
    proposals = hub.store.proposals()
    assert proposals[0]["status"] == "pending"
    assert answer.answer("Как включить роуминг?")["faq_id"] == 4   # бот не изменился


def test_недоставленное_в_telegram_помечается(hub):
    hub.store.ensure("777", title="Тест", source="telegram")
    result = hub.send("777", "Айгуль", "ответ")
    assert result["delivered"] is False
    assert hub.store.messages("777")[-1]["tag"] == "error"


def test_доставка_в_telegram_через_sender(hub):
    sent = []
    hub.set_sender(lambda chat, text: sent.append((chat, text)) or True)
    hub.store.ensure("777", title="Тест", source="telegram")
    hub.send("777", "Айгуль", "ответ")
    assert ("777", "ответ") in sent
    assert any("Айгуль" in text for _, text in sent)    # уведомление о подключении


def test_писать_от_имени_настоящего_абонента_нельзя(hub):
    hub.store.ensure("777", source="telegram")
    with pytest.raises(PermissionError):
        hub.demo_message("777", "привет")


# --- обучение -------------------------------------------------------------

PHRASE = "денежка на симке какая"


def test_обучение_учит_формулировке(hub):
    assert answer.answer(PHRASE)["faq_id"] != 1
    chat = demo(hub)
    hub.demo_message(chat, PHRASE)
    message_id = hub.store.unanswered()[0]["id"]
    verdict = hub.tag(message_id, 1, "Айгуль")
    assert verdict["accepted"], verdict
    assert verdict["after"]["top1"] >= verdict["before"]["top1"]
    assert answer.answer(PHRASE)["faq_id"] == 1
    assert hub.store.unanswered() == []


def test_отмена_выученной_фразы(hub):
    chat = demo(hub)
    hub.demo_message(chat, PHRASE)
    hub.tag(hub.store.unanswered()[0]["id"], 1, "Айгуль")
    learned_id = hub.store.learned()[0]["id"]
    hub.disable_learned(learned_id)
    assert knowledge.LEARNED == {}
    assert answer.answer(PHRASE)["faq_id"] != 1


def test_вопрос_открытого_набора_не_выучивается(hub):
    hub.store.ensure("x", source="demo")
    message_id = hub.store.add_message("x", st.IN, "Скока денег на счету?")
    verdict = hub.tag(message_id, 1, "Айгуль")
    assert not verdict["accepted"] and verdict["code"] == "dev_question"


def test_фраза_без_значимых_слов_отклоняется(hub):
    hub.store.ensure("x", source="demo")
    message_id = hub.store.add_message("x", st.IN, "ну как")
    verdict = hub.tag(message_id, 3, "Айгуль")
    assert not verdict["accepted"]


def test_ошибочная_отметка_не_портит_бота(hub):
    """Синоним темы 1 под видом темы 9 сбивает открытый набор — отклонить."""
    hub.store.ensure("x", source="demo")
    message_id = hub.store.add_message("x", st.IN, "баланс телефона проверить остаток")
    verdict = hub.tag(message_id, 9, "Айгуль")
    assert verdict["code"] in ("regression", "accepted_weak", "accepted")
    if verdict["accepted"]:
        assert verdict["after"]["top1"] >= verdict["before"]["top1"]
    else:
        assert knowledge.LEARNED == {}


def test_вне_базы_ничему_не_учит(hub):
    chat = demo(hub)
    hub.demo_message(chat, "Курс доллара на сегодня")
    verdict = hub.tag(hub.store.unanswered()[0]["id"], None, "Айгуль")
    assert verdict["code"] == "offtopic"
    assert knowledge.LEARNED == {}


def test_без_crm_answer_не_меняется():
    assert knowledge.LEARNED == {}
    assert answer.answer("Как проверить баланс?")["faq_id"] == 1


# --- аналитика ------------------------------------------------------------

def test_аналитика(hub, clock):
    chat = demo(hub)
    hub.demo_message(chat, "Как проверить баланс?")
    hub.demo_message(chat, "Курс доллара на сегодня")
    hub.demo_message(chat, "оператор")
    clock.now += 30
    hub.send(chat, "Айгуль", "Слушаю")
    data = hub.analytics()
    assert data["bot_answered"] >= 1 and data["bot_missed"] >= 1
    assert data["median_wait"] == pytest.approx(30)
    assert {row["reason"] for row in data["reasons"]} == {"direct_request"}


# --- HTTP -----------------------------------------------------------------

def client(base):
    """Отдельный браузер: свой набор cookie, своя сессия."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in ({"X-CRM": "1"} if headers is None else headers).items():
            request.add_header(key, value)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    return call


@pytest.fixture
def base(hub):
    server = build_server("127.0.0.1", 0, hub=hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def admin(base):
    call = client(base)
    status, me = call("POST", "/api/login", {"login": "admin", "password": "admin123!"})
    assert status == 200 and me["role"] == "admin"
    return call


def make_operator(admin, base, login="aigul", password="secret1"):
    status, body = admin("POST", "/api/operators", {
        "login": login, "name": "Айгуль", "password": password, "role": "operator"})
    assert status == 200, body
    call = client(base)
    status, me = call("POST", "/api/login", {"login": login, "password": password})
    assert status == 200 and me["role"] == "operator"
    return call, body["operator"]


def test_api_без_входа_закрыт(base):
    status, _ = client(base)("GET", "/api/conversations")
    assert status == 401


def test_api_неверный_пароль(base):
    status, _ = client(base)("POST", "/api/login", {"login": "admin", "password": "x"})
    assert status == 403


def test_api_post_без_заголовка_отклонён(admin):
    status, _ = admin("POST", "/api/logout", {}, headers={})
    assert status == 403


def test_пароль_хранится_хэшем(admin, hub):
    row = hub.store.operator_by_login("admin")
    assert "admin123!" not in row["password_hash"]
    assert row["password_hash"].startswith("pbkdf2_sha256$")


def test_оператор_не_управляет_учётными_записями(admin, base):
    operator, _ = make_operator(admin, base)
    assert operator("GET", "/api/operators")[0] == 403
    assert operator("POST", "/api/operators", {
        "login": "evil", "name": "x", "password": "123456"})[0] == 403
    assert operator("GET", "/api/proposals/export")[0] == 403
    assert operator("GET", "/api/conversations")[0] == 200


def test_отключённый_оператор_теряет_доступ(admin, base):
    operator, row = make_operator(admin, base)
    assert admin("POST", f"/api/operators/{row['id']}", {"active": False})[0] == 200
    assert operator("GET", "/api/conversations")[0] == 401
    status, _ = client(base)("POST", "/api/login", {"login": "aigul", "password": "secret1"})
    assert status == 403


def test_смена_пароля_закрывает_старые_сессии(admin, base):
    operator, row = make_operator(admin, base)
    assert admin("POST", f"/api/operators/{row['id']}", {"password": "newpass1"})[0] == 200
    assert operator("GET", "/api/conversations")[0] == 401
    status, _ = client(base)("POST", "/api/login", {"login": "aigul", "password": "newpass1"})
    assert status == 200


def test_администратор_не_отключает_себя(admin, hub):
    me = hub.store.operator_by_login("admin")
    status, body = admin("POST", f"/api/operators/{me['id']}", {"active": False})
    assert status == 400


def test_логин_уникален_и_проверяется(admin, base):
    make_operator(admin, base)
    assert admin("POST", "/api/operators", {
        "login": "aigul", "name": "x", "password": "123456"})[0] == 400
    assert admin("POST", "/api/operators", {
        "login": "Плохой логин", "name": "x", "password": "123456"})[0] == 400
    assert admin("POST", "/api/operators", {
        "login": "short", "name": "x", "password": "123"})[0] == 400


def test_api_полный_сценарий(admin, base, hub):
    operator, _ = make_operator(admin, base)
    status, boot = operator("GET", "/api/bootstrap")
    assert status == 200 and len(boot["faq"]) == 15 and boot["me"]["role"] == "operator"

    chat = demo(hub)
    hub.demo_message(chat, "оператор")

    _, listing = operator("GET", "/api/conversations")
    found = next(c for c in listing["conversations"] if c["chat_id"] == chat)
    assert found["status"] == "waiting"

    status, sent = operator("POST", f"/api/conversations/{chat}/send", {"text": "Слушаю вас"})
    assert status == 200 and sent["delivered"]

    _, messages = operator("GET", f"/api/conversations/{chat}/messages")
    assert messages["messages"][-1]["text"] == "Слушаю вас"
    assert messages["conversation"]["operator"] == "Айгуль"

    assert operator("POST", f"/api/conversations/{chat}/release", {})[0] == 200
    status, data = operator("GET", "/api/analytics")
    assert status == 200 and data["conversations"] >= 1


def test_проверка_точности_идёт_по_текстам_открытого_набора():
    from faq_bot.crm.learning import Learner

    cases = Learner().cases()
    assert len(cases) == 40
    assert cases[0] == ("Скока денег на счету?", 1)


def test_api_статика_сжата_и_с_политикой_безопасности(hub):
    server = build_server("127.0.0.1", 0, hub=hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/",
            headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.headers["Content-Encoding"] == "gzip"
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            etag = response.headers["ETag"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/",
            headers={"If-None-Match": etag})
        with pytest.raises(urllib.error.HTTPError) as cached:
            urllib.request.urlopen(request, timeout=10)
        assert cached.value.code == 304
    finally:
        server.shutdown()
        server.server_close()


def test_poll_просыпается_на_изменении(hub):
    version = hub.store.feed.version
    result = {}

    def wait():
        result["value"] = hub.store.feed.wait(version, 5)

    thread = threading.Thread(target=wait)
    thread.start()
    hub.store.ensure("p", source="demo")
    hub.store.feed.bump("p")
    thread.join(2)
    assert not thread.is_alive()
    assert "chat:p" in result["value"][1]


# --------------------------------------------------------------------------
# Один экземпляр на порт
# --------------------------------------------------------------------------
#
# Дефект пойман живым тестом, и он был тихим. Браузер разговаривал с
# одним экземпляром CRM, а Telegram вёл другой. База SQLite общая,
# поэтому очередь и переписка выглядели правильно — но ответ оператора
# не доходил: «бот не подключён к Telegram». Ни лог, ни интерфейс
# причину не показывали.
#
# На Windows мало выключить SO_REUSEADDR у себя: флаг разрешает перехват
# тому, кто уже слушает. Поэтому первый экземпляр просит исключительное
# владение портом.

def test_a_second_crm_refuses_to_start_on_a_busy_port():
    from faq_bot.crm import server as crm_server

    first = crm_server.build_server(host="127.0.0.1", port=0)
    try:
        port = first.server_address[1]

        with pytest.raises(crm_server.PortBusy) as refused:
            crm_server.build_server(host="127.0.0.1", port=port)

        assert str(port) in str(refused.value)
        assert "занят" in str(refused.value)
    finally:
        first.server_close()


def test_the_printed_address_is_the_real_port(capsys):
    """При port=0 в лог должен попасть выданный порт, а не ноль."""
    from faq_bot.crm import server as crm_server

    server = crm_server.build_server(host="127.0.0.1", port=0)
    try:
        printed = capsys.readouterr().out

        assert str(server.server_address[1]) in printed
        assert ":0" not in printed
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# Оценка работы бота попадает в сводку и в саму переписку
# --------------------------------------------------------------------------

def test_a_rating_lands_in_analytics_and_in_the_conversation():
    from faq_bot.crm.hub import Hub
    from faq_bot.crm.store import Store

    hub = Hub(Store(":memory:"))
    hub.client_message("777", "интернет не работает")

    hub.rate("777", 4)
    hub.rate("777", 2)
    hub.rate("888", 5)

    report = hub.analytics()
    assert report["rating_count"] == 3
    assert report["rating_avg"] == pytest.approx((4 + 2 + 5) / 3)
    assert report["rating_low"] == 1, "оценка 2 не засчитана как низкая"
    # Оператор, открыв диалог, обязан видеть, чем он кончился.
    assert "2 из 5" in hub.store.messages("777")[-1]["text"]


def test_no_ratings_means_no_average():
    """Пустая сводка не должна показывать ноль как оценку."""
    from faq_bot.crm.hub import Hub
    from faq_bot.crm.store import Store

    report = Hub(Store(":memory:")).analytics()

    assert report["rating_count"] == 0
    assert report["rating_avg"] is None


def test_ratings_are_purged_with_the_rest_of_the_history():
    """Оценка — такой же след разговора и живёт по тому же сроку."""
    from faq_bot.crm.hub import Hub
    from faq_bot.crm.store import Store

    store = Store(":memory:")
    hub = Hub(store, retention_days=30)
    hub.rate("777", 5)
    old = store.clock() - 40 * 86400
    store._run("UPDATE ratings SET created_at = ?", (old,))

    store.purge(30)

    assert store.scalar("SELECT COUNT(*) FROM ratings") == 0


def test_an_edited_interface_file_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    """Правка в app.js должна доезжать до браузера сама.

    Раньше файл читался ровно один раз за жизнь процесса. Это стоило
    получаса поисков: правка в интерфейсе была на диске, сервер отдавал
    старый файл, и выглядело это как «новая возможность не работает».
    Переменная CRM_DEV существовала, но знать о ней, чтобы увидеть
    собственную правку, — плохая сделка.
    """
    from faq_bot.crm import server as crm_server

    folder = tmp_path / "static"
    folder.mkdir()
    page = folder / "app.js"
    page.write_text("первая версия", encoding="utf-8")
    monkeypatch.setattr(crm_server, "STATIC_DIR", folder)
    monkeypatch.setattr(crm_server, "_static_cache", {})

    before = crm_server._static("app.js")
    page.write_text("вторая версия, подлиннее", encoding="utf-8")
    after = crm_server._static("app.js")

    assert before is not None and after is not None
    assert b"\xd0\xb2\xd1\x82\xd0\xbe\xd1\x80\xd0\xb0\xd1\x8f" in after[0]
    assert before[2] != after[2], "etag не изменился — браузер не перечитает"


def test_a_path_outside_the_interface_folder_is_refused():
    """Выход за папку интерфейса невозможен."""
    from faq_bot.crm import server as crm_server

    for name in ("../store.py", "../../.env", "..\\hub.py"):
        assert crm_server._static(name) is None
