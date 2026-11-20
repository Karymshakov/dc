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

@pytest.fixture
def http(hub):
    server = build_server("127.0.0.1", 0, hub=hub, password="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if headers is None:
            headers = {"X-CRM": "1"}
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    yield call
    server.shutdown()
    server.server_close()


def test_api_без_входа_закрыт(http):
    status, _ = http("GET", "/api/conversations")
    assert status == 401


def test_api_неверный_пароль(http):
    status, body = http("POST", "/api/login", {"name": "Айгуль", "password": "x"})
    assert status == 403


def test_api_post_без_заголовка_отклонён(http):
    http("POST", "/api/login", {"name": "Айгуль", "password": "secret"})
    status, _ = http("POST", "/api/demo/new", {}, headers={})
    assert status == 403


def test_api_полный_сценарий(http):
    status, me = http("POST", "/api/login", {"name": "Айгуль", "password": "secret"})
    assert status == 200 and me["name"] == "Айгуль"

    status, boot = http("GET", "/api/bootstrap")
    assert status == 200 and len(boot["faq"]) == 15

    _, created = http("POST", "/api/demo/new", {})
    chat = created["conversation"]["chat_id"]
    assert http("POST", f"/api/demo/{chat}/message", {"text": "оператор"})[0] == 200

    _, listing = http("GET", "/api/conversations")
    found = next(c for c in listing["conversations"] if c["chat_id"] == chat)
    assert found["status"] == "waiting"

    status, sent = http("POST", f"/api/conversations/{chat}/send", {"text": "Слушаю вас"})
    assert status == 200 and sent["delivered"]

    _, messages = http("GET", f"/api/conversations/{chat}/messages")
    assert messages["messages"][-1]["text"] == "Слушаю вас"
    assert messages["conversation"]["operator"] == "Айгуль"

    assert http("POST", f"/api/conversations/{chat}/release", {})[0] == 200
    status, data = http("GET", "/api/analytics")
    assert status == 200 and data["conversations"] >= 1


def test_проверка_точности_идёт_по_текстам_открытого_набора():
    from faq_bot.crm.learning import Learner

    cases = Learner().cases()
    assert len(cases) == 40
    assert cases[0] == ("Скока денег на счету?", 1)


def test_api_статика_сжата_и_с_политикой_безопасности(hub):
    server = build_server("127.0.0.1", 0, hub=hub, password="secret")
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
