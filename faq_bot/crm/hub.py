"""Логика CRM: кто ведёт диалог, что видит оператор, чему учится бот.

Диалог всегда в одном из трёх состояний:

    bot       ведёт бот, оператор только наблюдает
    waiting   абонент попросил человека (или бот сам передал), никто не взял
    operator  ведёт оператор; бот в этом чате молчит

Поле `waiting_since` — время самой старой реплики абонента, на которую
ещё не ответил человек. Интерфейс сортирует очередь именно по нему, а не
по последнему сообщению: абонент, который ждёт сорок минут, наверху,
и новые сообщения других чатов его не вытесняют.

Хаб не знает про Telegram. Доставку сообщения абоненту бот подключает
функцией `set_sender`; демонстрационные чаты обходятся без неё.
"""
from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any

from faq_bot import config
from faq_bot.crm import store as st
from faq_bot.crm.learning import Learner, Verdict, clean

logger = logging.getLogger(__name__)

Sender = Callable[[str, str], bool]

NOTICES: dict[str, dict[str, str]] = {
    "taken": {
        "ru": "👩‍💼 К диалогу подключился оператор {name}. Пишите, я на связи.",
        "ky": "👩‍💼 Маекке оператор {name} кошулду. Жазыңыз, байланыштамын.",
    },
    "released": {
        "ru": "🤖 Оператор завершил диалог. Снова отвечает бот — задайте вопрос.",
        "ky": "🤖 Оператор маекти аяктады. Эми кайра бот жооп берет — суроо бериңиз.",
    },
}

REASONS: dict[str, str] = {
    "direct_request": "Попросили оператора",
    "complaint": "Жалоба или претензия",
    "refund": "Возврат средств",
    "out_of_base": "Вопрос вне базы",
    "repeated_miss": "Бот не понял несколько раз",
    "not_helping": "Совет не помог",
    "manual": "Перехвачен оператором",
}

DEMO_NAMES = ("Айгерим", "Бакыт", "Нурлан", "Жылдыз", "Тимур", "Айпери",
              "Эрлан", "Мээрим", "Данияр", "Камила", "Азамат", "Сезим")


class Hub:
    """Точка входа и для бота, и для веб-сервера. Потокобезопасна."""

    def __init__(self, store: st.Store | None = None,
                 retention_days: float = 30.0) -> None:
        self.store = store or st.Store()
        self.learner = Learner()
        self._sender: Sender | None = None
        self._lock = threading.RLock()
        removed = self.store.purge(retention_days)
        if removed:
            logger.info("CRM: удалено %d сообщений старше %s дн.", removed,
                        retention_days)
        self._apply_learned()
        # Прогрев проверки обучения: первая отметка оператора не ждёт
        # построения ранжировщика и прогона открытого набора.
        threading.Thread(target=self._warm, name="crm-warm", daemon=True).start()

    def _warm(self) -> None:
        try:
            with self.learner.lock:
                self.learner._before(self.learner.mapping(self.store.learned()))
        except Exception:                      # pragma: no cover
            logger.debug("прогрев обучения не удался", exc_info=True)

    # --- подключение доставки ----------------------------------------------

    def set_sender(self, sender: Sender | None) -> None:
        self._sender = sender

    def _deliver(self, conversation: dict[str, Any], text: str) -> bool:
        if conversation["source"] != "telegram":
            return True                    # демо-чат: доставлять некуда
        if self._sender is None:
            return False
        try:
            return bool(self._sender(conversation["chat_id"], text))
        except Exception:
            logger.exception("CRM: не доставлено в чат %s", conversation["chat_id"])
            return False

    # --- состояние ---------------------------------------------------------

    def status(self, chat_id: int | str) -> str:
        found = self.store.conversation(str(chat_id))
        return found["status"] if found else st.BOT

    def is_human(self, chat_id: int | str) -> bool:
        """Ведёт ли диалог человек (бот должен молчать)."""
        return self.status(chat_id) in (st.WAITING, st.OPERATOR)

    def _touch(self, chat_id: str, direction: str, text: str, **extra: Any) -> None:
        self.store.update(chat_id, last_text=text[:200], last_dir=direction,
                          updated_at=self.store.clock(), **extra)
        self.store.feed.bump(chat_id)

    # --- со стороны бота ---------------------------------------------------

    def client_message(self, chat_id: int | str, text: str, title: str = "",
                       username: str = "", source: str = "telegram",
                       lang: str | None = None) -> int:
        """Реплика абонента. Возвращает id сообщения."""
        chat_id = str(chat_id)
        with self._lock:
            conversation = self.store.ensure(chat_id, title=title,
                                             username=username, source=source)
            message_id = self.store.add_message(chat_id, st.IN, text)
            extra: dict[str, Any] = {"unread": conversation["unread"] + 1}
            if lang:
                extra["lang"] = lang
            if (conversation["status"] in (st.WAITING, st.OPERATOR)
                    and conversation["waiting_since"] is None):
                extra["waiting_since"] = self.store.clock()
            self._touch(chat_id, st.IN, text, **extra)
            return message_id

    def bot_reply(self, chat_id: int | str, text: str, faq_id: int | None = None,
                  confidence: float | None = None, searched: bool = False,
                  lang: str | None = None,
                  buttons: list[list[str]] | None = None,
                  replace: bool = False) -> int:
        """Ответ бота.

        `searched=True` значит, что ответ — результат поиска по базе. Тогда
        пустой `faq_id` помечает последнюю реплику абонента как непонятую,
        и она попадает в очередь обучения. Приветствия и меню не помечаются.
        `replace=True` переписывает прошлый ответ бота (дозревание, кнопки).
        """
        chat_id = str(chat_id)
        with self._lock:
            conversation = self.store.ensure(chat_id)
            incoming = self.store.last_message(chat_id, st.IN)
            last = self.store.last_message(chat_id)
            if (replace and last is not None and last["dir"] == st.OUT_BOT):
                self.store.edit_message(last["id"], text=text, faq_id=faq_id,
                                        confidence=confidence, buttons=buttons)
                message_id = last["id"]
            else:
                message_id = self.store.add_message(
                    chat_id, st.OUT_BOT, text, author="bot", faq_id=faq_id,
                    confidence=confidence, buttons=buttons)
            extra: dict[str, Any] = {}
            if lang:
                extra["lang"] = lang
            if searched and incoming is not None and incoming["tag"] is None:
                missed = faq_id is None
                self.store.edit_message(incoming["id"], unanswered=missed,
                                        faq_id=faq_id, confidence=confidence)
                extra["bot_misses"] = conversation["bot_misses"] + (1 if missed else 0)
                if faq_id is not None:
                    extra["topic"] = faq_id
            self._touch(chat_id, st.OUT_BOT, text, **extra)
            return message_id

    def request_operator(self, chat_id: int | str, reason: str | None) -> None:
        """Бот передаёт диалог человеку: диалог встаёт в очередь."""
        chat_id = str(chat_id)
        with self._lock:
            conversation = self.store.ensure(chat_id)
            if conversation["status"] == st.OPERATOR:
                return
            code = reason or "direct_request"
            waiting = conversation["waiting_since"] or self.store.clock()
            self.store.add_message(chat_id, st.SYSTEM,
                                   f"Передан оператору: {REASONS.get(code, code)}",
                                   tag=f"handoff:{code}")
            self._touch(chat_id, st.SYSTEM, "Ждёт оператора", status=st.WAITING,
                        reason=code, waiting_since=waiting)

    # --- со стороны оператора ----------------------------------------------

    def take(self, chat_id: str, operator: str, notify: bool = True) -> dict[str, Any]:
        """Оператор перехватывает диалог. Бот в этом чате замолкает."""
        with self._lock:
            conversation = self._require(chat_id)
            if (conversation["status"] == st.OPERATOR
                    and conversation["operator"] == operator):
                return conversation
            previous = conversation["operator"]
            reason = conversation["reason"] or "manual"
            note = (f"{operator} забрал(а) диалог у {previous}" if previous
                    and previous != operator else f"{operator} взял(а) диалог")
            self.store.add_message(chat_id, st.SYSTEM, note, author=operator,
                                   tag="take" if conversation["reason"] else "handoff:manual")
            self._touch(chat_id, st.SYSTEM, note, status=st.OPERATOR,
                        operator=operator, reason=reason)
            if notify:
                lang = conversation["lang"] if conversation["lang"] in config.LANGUAGES else "ru"
                self._deliver(conversation, NOTICES["taken"][lang].format(name=operator))
            return self._require(chat_id)

    def release(self, chat_id: str, operator: str, notify: bool = True) -> dict[str, Any]:
        """Вернуть диалог боту."""
        with self._lock:
            conversation = self._require(chat_id)
            if conversation["status"] == st.BOT:
                return conversation
            note = f"{operator} вернул(а) диалог боту"
            self.store.add_message(chat_id, st.SYSTEM, note, author=operator,
                                   tag="release")
            self._touch(chat_id, st.SYSTEM, note, status=st.BOT, operator=None,
                        reason=None, waiting_since=None, unread=0, bot_misses=0)
            if notify:
                lang = conversation["lang"] if conversation["lang"] in config.LANGUAGES else "ru"
                self._deliver(conversation, NOTICES["released"][lang])
            self._forget_bot_memory(chat_id)
            return self._require(chat_id)

    def send(self, chat_id: str, operator: str, text: str,
             propose: bool = False, faq_id: int | None = None) -> dict[str, Any]:
        """Ответ оператора абоненту. Диалог при этом автоматически за ним."""
        text = (text or "").strip()
        if not text:
            raise ValueError("пустое сообщение")
        if len(text) > 4000:
            raise ValueError("сообщение длиннее 4000 знаков")
        with self._lock:
            conversation = self._require(chat_id)
            if (conversation["status"] != st.OPERATOR
                    or conversation["operator"] != operator):
                conversation = self.take(chat_id, operator)
            delivered = self._deliver(conversation, text)
            waiting = conversation["waiting_since"]
            wait = self.store.clock() - waiting if waiting else None
            message_id = self.store.add_message(chat_id, st.OUT_OP, text,
                                                author=operator, faq_id=faq_id,
                                                wait=wait)
            if not delivered:
                self.store.add_message(chat_id, st.SYSTEM,
                                       "Не доставлено: бот не подключён к Telegram",
                                       tag="error")
            proposal = None
            if propose:
                question = self.store.last_message(chat_id, st.IN)
                proposal = self.store.add_proposal(
                    question=question["text"] if question else "",
                    answer=text, lang=conversation["lang"], faq_id=faq_id,
                    operator=operator, chat_id=chat_id)
                self.store.feed.bump(None, "learning")
            self._touch(chat_id, st.OUT_OP, text, waiting_since=None, unread=0)
            return {"message_id": message_id, "delivered": delivered,
                    "proposal_id": proposal}

    def mark_read(self, chat_id: str) -> None:
        conversation = self._require(chat_id)
        if conversation["unread"]:
            self.store.update(chat_id, unread=0)
            self.store.feed.bump(chat_id)

    def _require(self, chat_id: str) -> dict[str, Any]:
        found = self.store.conversation(str(chat_id))
        if found is None:
            raise KeyError(chat_id)
        return found

    @staticmethod
    def _forget_bot_memory(chat_id: str) -> None:
        """После оператора бот начинает разговор с чистого листа."""
        try:
            from faq_bot import bot

            bot.forget_chat(chat_id)
        except Exception:                      # pragma: no cover
            logger.debug("память бота для %s не сброшена", chat_id)

    # --- обучение ----------------------------------------------------------

    def _apply_learned(self) -> None:
        self.learner.apply(self.learner.mapping(self.store.learned()))

    def tag(self, message_id: int, faq_id: int | None, operator: str) -> dict[str, Any]:
        """Отметка оператора: реплика относится к теме (или вне базы)."""
        message = self.store.message(message_id)
        if message is None or message["dir"] != st.IN:
            raise KeyError(message_id)
        if faq_id is None:
            self.store.edit_message(message_id, tag="offtopic")
            self.store.feed.bump(message["chat_id"])
            self.store.feed.bump(None, "learning")
            return {"accepted": True, "code": "offtopic",
                    "message": "Отмечено как вопрос вне базы. Бот этому не учится."}
        with self.learner.lock:
            current = self.learner.mapping(self.store.learned())
            verdict: Verdict = self.learner.check(message["text"], int(faq_id), current)
            if verdict.accepted:
                self.store.add_learned(clean(message["text"]), int(faq_id), operator,
                                       message_id, verdict.as_dict())
                self._apply_learned()
                self.store.edit_message(message_id, tag=str(faq_id))
            elif verdict.code == "already_known":
                self.store.edit_message(message_id, tag=str(faq_id))
        self.store.feed.bump(message["chat_id"])
        self.store.feed.bump(None, "learning")
        return verdict.as_dict()

    def disable_learned(self, learned_id: int) -> None:
        with self.learner.lock:
            self.store.set_learned_active(learned_id, False)
            self._apply_learned()
        self.store.feed.bump(None, "learning")

    def review(self, proposal_id: int, status: str, reviewer: str) -> None:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError(status)
        self.store.review_proposal(proposal_id, status, reviewer)
        self.store.feed.bump(None, "learning")

    def learning_state(self) -> dict[str, Any]:
        from faq_bot import ranking

        queue = self.store.unanswered(limit=100)
        for item in queue:
            try:
                item["suggest"] = [match.faq_id for match in ranking.rank(item["text"])]
            except Exception:                  # pragma: no cover
                item["suggest"] = []
        return {
            "queue": queue,
            "learned": self.store.learned(active_only=True),
            "proposals": self.store.proposals(),
        }

    # --- абонент без Telegram (для тестов) ---------------------------------

    def new_demo_chat(self) -> dict[str, Any]:
        chat_id = f"demo-{secrets.token_hex(3)}"
        name = DEMO_NAMES[secrets.randbelow(len(DEMO_NAMES))]
        self.store.ensure(chat_id, title=name, username="", source="demo")
        self.store.feed.bump(chat_id)
        return self._require(chat_id)

    def demo_message(self, chat_id: str, text: str) -> None:
        """Реплика тестового абонента: ровно тот путь, что из Telegram."""
        from faq_bot import bot

        conversation = self._require(chat_id)
        if conversation["source"] != "demo":
            raise PermissionError("писать от имени абонента можно только в демо-чат")
        text = (text or "").strip()[:1000]
        if not text:
            raise ValueError("пустое сообщение")
        bot.crm_client_turn(self, chat_id, text, title=conversation["title"],
                            source="demo")

    def demo_action(self, chat_id: str, action: str) -> None:
        from faq_bot import bot

        conversation = self._require(chat_id)
        if conversation["source"] != "demo":
            raise PermissionError("только демо-чат")
        bot.crm_button(self, chat_id, action)

    # --- аналитика ---------------------------------------------------------

    def analytics(self) -> dict[str, Any]:
        s = self.store
        now = s.clock()
        day = now - 86400
        waits = [row["wait"] for row in s.rows(
            "SELECT wait FROM messages WHERE dir = 'op' AND wait IS NOT NULL"
            " AND created_at > ?", (now - 7 * 86400,))]
        waits.sort()
        answered = s.scalar("SELECT COUNT(*) FROM messages WHERE dir = 'in'"
                            " AND faq_id IS NOT NULL AND created_at > ?", (day,)) or 0
        missed = s.scalar("SELECT COUNT(*) FROM messages WHERE dir = 'in'"
                          " AND unanswered = 1 AND created_at > ?", (day,)) or 0
        hours = [0] * 24
        for row in s.rows("SELECT created_at FROM messages WHERE dir = 'in'"
                          " AND created_at > ?", (day,)):
            index = int((row["created_at"] - day) // 3600)
            hours[min(23, max(0, index))] += 1
        oldest = s.scalar("SELECT MIN(waiting_since) FROM conversations"
                          " WHERE waiting_since IS NOT NULL")
        return {
            "now": now,
            "conversations": s.scalar("SELECT COUNT(*) FROM conversations") or 0,
            "active_today": s.scalar("SELECT COUNT(*) FROM conversations"
                                     " WHERE updated_at > ?", (day,)) or 0,
            "waiting": s.scalar("SELECT COUNT(*) FROM conversations"
                                " WHERE waiting_since IS NOT NULL") or 0,
            "with_operator": s.scalar("SELECT COUNT(*) FROM conversations"
                                      " WHERE status = 'operator'") or 0,
            "oldest_wait": (now - oldest) if oldest else 0,
            "messages_today": s.scalar("SELECT COUNT(*) FROM messages WHERE dir = 'in'"
                                       " AND created_at > ?", (day,)) or 0,
            "bot_answered": answered,
            "bot_missed": missed,
            "median_wait": waits[len(waits) // 2] if waits else None,
            "p90_wait": waits[int(len(waits) * 0.9)] if waits else None,
            "hours": hours,
            "topics": s.rows("SELECT faq_id, COUNT(*) AS n FROM messages"
                             " WHERE dir = 'in' AND faq_id IS NOT NULL"
                             " GROUP BY faq_id ORDER BY n DESC"),
            "languages": s.rows("SELECT lang, COUNT(*) AS n FROM conversations"
                                " GROUP BY lang ORDER BY n DESC"),
            "reasons": s.rows("SELECT substr(tag, 9) AS reason, COUNT(*) AS n"
                              " FROM messages WHERE tag LIKE 'handoff:%'"
                              " GROUP BY reason ORDER BY n DESC"),
            "operators": s.rows("SELECT author AS name, COUNT(*) AS n,"
                                " AVG(wait) AS avg_wait FROM messages"
                                " WHERE dir = 'op' GROUP BY author ORDER BY n DESC"),
            "learned": s.scalar("SELECT COUNT(*) FROM learned WHERE active = 1") or 0,
            "queue": s.scalar("SELECT COUNT(*) FROM messages"
                              " WHERE unanswered = 1 AND tag IS NULL") or 0,
            "proposals_pending": s.scalar("SELECT COUNT(*) FROM proposals"
                                          " WHERE status = 'pending'") or 0,
        }
