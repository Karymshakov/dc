/* INAI Mobile CRM — интерфейс оператора.
 *
 * Без фреймворков и сборки. Весь пользовательский текст попадает в DOM
 * только через textContent: разметку из сообщений абонентов не исполняем.
 *
 * Обновления приходят длинным опросом /api/poll: сервер держит запрос,
 * пока ничего не изменилось, и отвечает сразу, как только изменилось.
 */
"use strict";

const S = {
  me: null,
  faq: new Map(),
  convs: new Map(),
  msgs: new Map(),          // chat_id -> массив сообщений по возрастанию id
  open: null,
  view: "chats",
  filter: "all",
  search: "",
  version: 0,
  offset: 0,                // серверное время минус локальное, секунды
  collapsed: new Set(),
  demoChat: null,
  hasOlder: new Map(),
};

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else el.setAttribute(key, value === true ? "" : value);
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) svg.setAttribute("class", cls);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("crm." + key); return v == null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("crm." + key, JSON.stringify(value)); } catch { /* приватный режим */ } },
};

/* ---------------- сеть ---------------- */

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const options = { method, headers: { "X-CRM": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch { /* пустой ответ */ }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new HttpError(response.status, (data && data.error) || response.statusText);
  }
  return data;
}

const now = () => Date.now() / 1000 + S.offset;

/* ---------------- форматирование ---------------- */

const LANG = { ru: "RU", ky: "KY" };
const REASONS = {
  direct_request: "Попросили оператора", complaint: "Жалоба", refund: "Возврат средств",
  out_of_base: "Вопрос вне базы", repeated_miss: "Бот не понял несколько раз",
  not_helping: "Совет не помог", manual: "Перехвачен оператором",
};

function hue(text) {
  let x = 0;
  for (const ch of String(text)) x = (x * 31 + ch.codePointAt(0)) % 360;
  return x;
}

function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

function displayName(conv) {
  if (conv.title) return conv.title;
  if (conv.username) return "@" + conv.username;
  return "Абонент " + String(conv.chat_id).slice(-4);
}

function avatar(conv, cls = "") {
  const el = h("span", { class: "avatar " + cls, text: initials(displayName(conv)) });
  el.style.setProperty("--h", hue(conv.chat_id));
  if (conv.status) el.append(h("i", { class: "st " + conv.status }));
  return el;
}

function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return seconds + " с";
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + " мин";
  const hrs = Math.floor(m / 60);
  if (hrs < 24) return hrs + " ч " + String(m % 60).padStart(2, "0") + " мин";
  return Math.floor(hrs / 24) + " д " + (hrs % 24) + " ч";
}

function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function shortTime(ts) {
  const date = new Date(ts * 1000);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return clock(ts);
  const diff = (today - date) / 86400000;
  if (diff < 6) return date.toLocaleDateString("ru-RU", { weekday: "short" });
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function dayLabel(ts) {
  const date = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (date.toDateString() === today.toDateString()) return "Сегодня";
  if (date.toDateString() === yesterday.toDateString()) return "Вчера";
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

function slaClass(seconds) {
  if (seconds < 300) return "s-ok";
  if (seconds < 900) return "s-warn";
  return "s-bad";
}

function topicName(id, lang = "ru") {
  const entry = S.faq.get(Number(id));
  if (!entry) return "Тема " + id;
  return lang === "ky" ? entry.question_ky : entry.question_ru;
}

function percent(value) { return Math.round((value || 0) * 100) + "%"; }

/* ---------------- уведомления ---------------- */

function toast(text, kind = "ok", detail = "") {
  const el = h("div", { class: "toast " + kind },
    icon(kind === "bad" ? "x" : "check"),
    h("div", null, text, detail ? h("small", { text: detail }) : null));
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 220); }, kind === "bad" ? 6000 : 3800);
}

function fail(error) {
  console.warn(error);
  toast(error.message || "Ошибка", "bad");
}

/* ---------------- тема ---------------- */

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#theme-toggle use").setAttribute("href", dark ? "#i-sun" : "#i-moon");
}

$("#theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  store.set("theme", next);
  applyTheme(next);
});

/* ---------------- вход ---------------- */

function showLogin() {
  $("#app").hidden = true;
  $("#demo").hidden = true;
  $("#login").hidden = false;
  const name = store.get("name", "");
  const form = $("#login-form");
  if (name) form.elements.name.value = name;
  (name ? form.elements.password : form.elements.name).focus();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  $("#login-error").textContent = "";
  button.disabled = true;
  try {
    const me = await api("POST", "/api/login", {
      name: form.elements.name.value, password: form.elements.password.value,
    });
    store.set("name", me.name);
    form.elements.password.value = "";
    await start();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout", {}); } catch { /* всё равно выходим */ }
  location.reload();
});

/* ---------------- запуск и опрос ---------------- */

let polling = false;

async function start() {
  const data = await api("GET", "/api/bootstrap");
  S.me = data.me;
  S.version = data.version;
  S.offset = data.now - Date.now() / 1000;
  S.faq = new Map(data.faq.map((entry) => [entry.id, entry]));
  setConversations(data.conversations);
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#me-name").textContent = S.me;
  S.collapsed = new Set(store.get("collapsed", []));
  setView(store.get("view", "chats"));
  renderList();
  if (!polling) { polling = true; poll(); }
}

async function poll() {
  for (;;) {
    try {
      const data = await api("GET", "/api/poll?v=" + S.version);
      S.offset = data.now - Date.now() / 1000;
      if (data.version !== S.version) {
        S.version = data.version;
        onChanged(data.changed);
      }
    } catch (error) {
      if (error.status === 401) { polling = false; return; }
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
  }
}

const debounced = new Map();
function soon(key, fn, delay = 120) {
  clearTimeout(debounced.get(key));
  debounced.set(key, setTimeout(fn, delay));
}

function onChanged(changed) {
  const everything = changed.includes("*");
  const chats = new Set(changed.filter((key) => key.startsWith("chat:")).map((key) => key.slice(5)));
  if (everything || chats.size) soon("list", refreshList);
  if (S.open && (everything || chats.has(S.open))) soon("open", () => loadMessages(S.open, true));
  if (S.demoChat && (everything || chats.has(S.demoChat))) soon("demo", renderDemo, 60);
  if (S.view === "learning" && (everything || changed.some((key) => key.startsWith("learning")) || chats.size)) soon("learning", loadLearning, 250);
  if (S.view === "analytics") soon("analytics", loadAnalytics, 600);
}

async function refreshList() {
  try {
    const data = await api("GET", "/api/conversations");
    S.offset = data.now - Date.now() / 1000;
    setConversations(data.conversations);
    renderList();
    if (S.open) renderChatHead();
  } catch (error) { if (error.status !== 401) console.warn(error); }
}

function setConversations(rows) {
  const previous = S.convs;
  S.convs = new Map(rows.map((row) => [row.chat_id, row]));
  S.flash = new Set();
  for (const row of rows) {
    const old = previous.get(row.chat_id);
    if (old && row.updated_at > old.updated_at && row.last_dir === "in" && row.chat_id !== S.open) S.flash.add(row.chat_id);
  }
  updateBadges();
}

function updateBadges() {
  let waiting = 0;
  for (const conv of S.convs.values()) if (conv.waiting_since) waiting++;
  const badge = $("#badge-waiting");
  badge.hidden = !waiting;
  badge.textContent = waiting;
  document.title = (waiting ? `(${waiting}) ` : "") + "INAI Mobile · CRM";
}

/* ---------------- разделы ---------------- */

function setView(view) {
  if (!["chats", "learning", "analytics"].includes(view)) view = "chats";
  S.view = view;
  store.set("view", view);
  for (const pane of document.querySelectorAll(".view")) pane.hidden = pane.dataset.pane !== view;
  for (const button of document.querySelectorAll(".rail-btn[data-view]")) button.classList.toggle("active", button.dataset.view === view);
  if (view === "learning") loadLearning();
  if (view === "analytics") loadAnalytics();
}

for (const button of document.querySelectorAll(".rail-btn[data-view]")) {
  button.addEventListener("click", () => setView(button.dataset.view));
}

/* ---------------- список диалогов ---------------- */

const GROUPS = [
  { key: "waiting", title: "Ждут ответа", empty: "Никто не ждёт — отлично." },
  { key: "work", title: "В работе", empty: "Нет диалогов у операторов." },
  { key: "bot", title: "Ведёт бот", empty: "Пока пусто." },
];

function groupOf(conv) {
  if (conv.waiting_since) return "waiting";
  if (conv.status === "operator") return "work";
  return "bot";
}

function visible(conv) {
  if (S.filter === "mine" && conv.operator !== S.me) return false;
  if ((S.filter === "ky" || S.filter === "ru") && conv.lang !== S.filter) return false;
  if (S.search) {
    const hay = (displayName(conv) + " " + conv.username + " " + conv.last_text).toLowerCase();
    if (!hay.includes(S.search)) return false;
  }
  return true;
}

function sortedGroup(key) {
  const rows = [...S.convs.values()].filter((conv) => groupOf(conv) === key && visible(conv));
  if (key === "waiting") rows.sort((a, b) => a.waiting_since - b.waiting_since);   // дольше всех ждёт — сверху
  else rows.sort((a, b) => b.updated_at - a.updated_at);
  return rows;
}

function convItem(conv) {
  const name = displayName(conv);
  const top = h("span", { class: "conv-top" },
    h("span", { class: "conv-name", text: name }),
    h("span", { class: "lang " + conv.lang, text: LANG[conv.lang] || conv.lang }));
  const prefix = { bot: "Бот: ", op: (conv.operator || "Оператор") + ": ", sys: "" }[conv.last_dir] || "";
  const last = h("span", { class: "conv-last" }, prefix ? h("b", { text: prefix }) : null, conv.last_text || "—");
  const side = h("span", { class: "conv-side" });
  if (conv.waiting_since) {
    const since = now() - conv.waiting_since;
    side.append(h("span", { class: "sla " + slaClass(since), dataset: { since: conv.waiting_since } }, icon("clock"), h("span", { text: duration(since) })));
  } else {
    side.append(h("span", { class: "conv-time", text: shortTime(conv.updated_at) }));
  }
  if (conv.unread) side.append(h("span", { class: "unread", text: conv.unread > 99 ? "99+" : conv.unread }));
  const item = h("button", {
    class: "conv" + (conv.chat_id === S.open ? " active" : "") + (S.flash && S.flash.has(conv.chat_id) ? " flash" : ""),
    dataset: { id: conv.chat_id }, title: conv.reason ? REASONS[conv.reason] || conv.reason : null,
  }, avatar(conv), h("span", { class: "conv-main" }, top, last), side);
  return item;
}

function renderList() {
  const list = $("#conv-list");
  const scroll = list.scrollTop;
  const fragment = document.createDocumentFragment();
  for (const group of GROUPS) {
    const rows = sortedGroup(group.key);
    const collapsed = S.collapsed.has(group.key);
    const section = h("section", { class: `group g-${group.key}` + (collapsed ? " collapsed" : "") },
      h("button", { class: "group-head", dataset: { group: group.key } },
        icon("chevron"), h("span", { class: "dot" }), group.title, h("span", { class: "n", text: rows.length })),
      h("div", { class: "group-body" },
        rows.length ? rows.map(convItem) : h("div", { class: "group-empty", text: group.empty })));
    fragment.append(section);
  }
  list.replaceChildren(fragment);
  list.scrollTop = scroll;
}

$("#conv-list").addEventListener("click", (event) => {
  const head = event.target.closest(".group-head");
  if (head) {
    const key = head.dataset.group;
    if (S.collapsed.has(key)) S.collapsed.delete(key); else S.collapsed.add(key);
    store.set("collapsed", [...S.collapsed]);
    renderList();
    return;
  }
  const item = event.target.closest(".conv");
  if (item) openChat(item.dataset.id);
});

$("#search").addEventListener("input", (event) => {
  S.search = event.target.value.trim().toLowerCase();
  soon("search", renderList, 80);
});

$("#filters").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  S.filter = chip.dataset.filter;
  for (const other of $("#filters").children) other.classList.toggle("active", other === chip);
  renderList();
});

/* Таймеры ожидания обновляются раз в секунду без перерисовки списка. */
setInterval(() => {
  for (const el of document.querySelectorAll("[data-since]")) {
    const since = now() - Number(el.dataset.since);
    const cls = slaClass(since);
    if (!el.classList.contains(cls)) el.className = el.className.replace(/\bs-\w+/, cls);
    const label = el.querySelector("span") || el;
    const text = duration(since);
    if (label.textContent !== text) label.textContent = text;
  }
}, 1000);

/* ---------------- открытый диалог ---------------- */

async function openChat(chatId) {
  if (!S.convs.has(chatId)) return;
  S.open = chatId;
  $("#chat-empty").hidden = true;
  $("#chat").hidden = false;
  $(".view-chats").classList.add("chat-open");
  if (S.view !== "chats") setView("chats");
  for (const item of document.querySelectorAll(".conv")) item.classList.toggle("active", item.dataset.id === chatId);
  renderChatHead();
  $("#messages").replaceChildren();
  S.msgs.delete(chatId);
  await loadMessages(chatId, false);
  if (!$("#info-pane").hidden) renderInfo();
  if (matchMedia("(min-width: 761px)").matches) $("#reply").focus();
}

$("#chat-back").addEventListener("click", () => {
  $(".view-chats").classList.remove("chat-open");
  S.open = null;
  renderList();
});

function renderChatHead() {
  const conv = S.convs.get(S.open);
  if (!conv) return;
  $("#chat-avatar").replaceWith(Object.assign(avatar(conv, "lg"), { id: "chat-avatar" }));
  $("#chat-name").textContent = displayName(conv);
  const lang = $("#chat-lang");
  lang.textContent = LANG[conv.lang] || conv.lang;
  lang.className = "lang " + conv.lang;
  $("#chat-src").textContent = conv.source === "demo" ? "демо" : (conv.username ? "@" + conv.username : "Telegram");

  const status = $("#chat-status");
  const pills = [];
  if (conv.status === "operator") pills.push(h("span", { class: "pill operator" }, icon("user"), "Ведёт " + (conv.operator === S.me ? "вы" : conv.operator)));
  else if (conv.status === "waiting") pills.push(h("span", { class: "pill waiting" }, icon("hand"), "Ждёт оператора"));
  else pills.push(h("span", { class: "pill bot" }, icon("bot"), "Ведёт бот"));
  if (conv.reason) pills.push(h("span", { text: REASONS[conv.reason] || conv.reason }));
  if (conv.waiting_since) pills.push(h("span", { class: "sla " + slaClass(now() - conv.waiting_since), dataset: { since: conv.waiting_since } }, icon("clock"), h("span", { text: duration(now() - conv.waiting_since) })));
  status.replaceChildren(...pills);

  const mine = conv.status === "operator" && conv.operator === S.me;
  const take = $("#btn-take");
  take.hidden = mine;
  take.querySelector("span").textContent = conv.status === "operator" ? "Забрать себе" : "Перехватить";
  $("#btn-release").hidden = conv.status === "bot";

  const note = $("#composer-note");
  note.className = "composer-note";
  if (conv.status === "bot") {
    note.classList.add("bot");
    note.replaceChildren(icon("bot"), "Диалог ведёт бот. Ваш ответ перехватит диалог — бот в этом чате замолчит.");
  } else if (conv.status === "operator" && !mine) {
    note.replaceChildren(icon("info"), `Диалог ведёт ${conv.operator}. Ответ заберёт его себе.`);
  } else if (conv.source === "demo") {
    note.replaceChildren(icon("phone"), "Демо-абонент: ответ появится в окне «Демо-клиент».");
  } else {
    note.replaceChildren();
  }
}

async function loadMessages(chatId, incremental) {
  const known = S.msgs.get(chatId) || [];
  const lastId = known.length ? known[known.length - 1].id : 0;
  // Догружаем с небольшим нахлёстом: так видны и правки последних
  // ответов бота (дозревание переписывает сообщение с тем же id).
  const after = incremental && lastId ? Math.max(0, lastId - 4) : 0;
  let data;
  try {
    data = await api("GET", `/api/conversations/${encodeURIComponent(chatId)}/messages?after=${after}`);
  } catch (error) { if (error.status !== 401) fail(error); return; }
  if (chatId !== S.open) return;
  if (data.conversation) S.convs.set(chatId, data.conversation);
  let merged;
  if (!incremental || !lastId) {
    merged = data.messages;
    S.hasOlder.set(chatId, data.messages.length >= 80);
  } else {
    const byId = new Map(known.map((m) => [m.id, m]));
    for (const m of data.messages) byId.set(m.id, m);
    merged = [...byId.values()].sort((a, b) => a.id - b.id);
  }
  S.msgs.set(chatId, merged);
  renderMessages(!incremental || !lastId);
  renderChatHead();
  const conv = S.convs.get(chatId);
  if (conv && conv.unread && document.visibilityState === "visible") {
    api("POST", `/api/conversations/${encodeURIComponent(chatId)}/read`, {}).catch(() => {});
  }
}

async function loadOlder() {
  const chatId = S.open;
  const known = S.msgs.get(chatId) || [];
  if (!known.length) return;
  const box = $("#messages");
  const height = box.scrollHeight;
  const data = await api("GET", `/api/conversations/${encodeURIComponent(chatId)}/messages?before=${known[0].id}`);
  if (chatId !== S.open) return;
  S.hasOlder.set(chatId, data.messages.length >= 80);
  S.msgs.set(chatId, [...data.messages, ...known]);
  renderMessages(false);
  box.scrollTop = box.scrollHeight - height;
}

function messageNode(m, prev, conv) {
  if (m.dir === "sys") {
    const cls = m.tag && m.tag.startsWith("handoff") ? " handoff" : m.tag === "error" ? " error" : "";
    return h("div", { class: "sys" + cls }, icon(cls === " handoff" ? "hand" : "info"), m.text, h("span", { class: "muted", text: "· " + clock(m.created_at) }));
  }
  const side = m.dir === "in" ? "in" : "out";
  const cont = prev && prev.dir === m.dir && prev.author === m.author && m.created_at - prev.created_at < 300;
  const row = h("div", { class: `row ${side} ${m.dir}` + (cont ? " cont" : ""), dataset: { id: m.id } });
  if (!cont) {
    if (m.dir === "in") row.append(h("div", { class: "who" }, displayName(conv)));
    else if (m.dir === "bot") row.append(h("div", { class: "who" }, icon("bot"), "Бот"));
    else row.append(h("div", { class: "who" }, icon("user"), m.author || "Оператор"));
  }
  const bubble = h("div", { class: "bubble" + (m.dir === "in" && m.unanswered && !m.tag ? " missed" : ""), text: m.text });
  if (m.dir === "bot" && m.buttons && m.buttons.length) {
    bubble.append(h("div", { class: "btns" }, m.buttons.slice(0, 8).map(([label]) => h("span", { text: label }))));
  }
  row.append(bubble);

  const meta = h("div", { class: "meta" }, h("span", { text: clock(m.created_at) }));
  if (m.dir === "bot" && m.faq_id) {
    meta.append(h("span", { class: "tagchip topic", title: topicName(m.faq_id) }, "Тема " + m.faq_id, m.confidence != null ? h("span", { class: "conf", text: " · " + percent(m.confidence) }) : null));
  }
  if (m.dir === "in") {
    if (m.tag === "offtopic") meta.append(h("span", { class: "tagchip off", text: "вне базы" }));
    else if (m.tag) meta.append(h("span", { class: "tagchip done" }, icon("check"), "Тема " + m.tag));
    else if (m.unanswered) {
      meta.append(h("span", { class: "tagchip miss", text: "бот не понял" }));
      meta.append(h("button", { class: "tagchip teach", dataset: { teach: m.id } }, icon("spark"), "Научить"));
    } else if (m.faq_id) {
      meta.append(h("span", { class: "tagchip topic", text: "Тема " + m.faq_id }));
    }
  }
  if (m.dir === "op" && m.wait) meta.append(h("span", { text: "ответ через " + duration(m.wait) }));
  row.append(meta);
  return row;
}

function renderMessages(scrollToEnd) {
  const box = $("#messages");
  const conv = S.convs.get(S.open);
  const list = S.msgs.get(S.open) || [];
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const fragment = document.createDocumentFragment();
  if (S.hasOlder.get(S.open)) fragment.append(h("button", { class: "btn ghost sm load-more", onclick: () => loadOlder().catch(fail) }, "Показать раньше"));
  if (!list.length) fragment.append(h("div", { class: "empty" }, h("b", { text: "Сообщений пока нет" }), "Они появятся здесь, как только абонент напишет."));
  let prev = null;
  let day = "";
  for (const m of list) {
    const label = dayLabel(m.created_at);
    if (label !== day) { fragment.append(h("div", { class: "day", text: label })); day = label; prev = null; }
    fragment.append(messageNode(m, prev, conv));
    prev = m.dir === "sys" ? null : m;
  }
  box.replaceChildren(fragment);
  if (scrollToEnd || atBottom) box.scrollTop = box.scrollHeight;
}

$("#messages").addEventListener("click", (event) => {
  const teach = event.target.closest("[data-teach]");
  if (teach) openTopicPicker(teach, Number(teach.dataset.teach), []);
});

/* ---------------- действия оператора ---------------- */

async function convAction(action) {
  const chatId = S.open;
  try {
    const data = await api("POST", `/api/conversations/${encodeURIComponent(chatId)}/${action}`, {});
    S.convs.set(chatId, data.conversation);
    renderChatHead();
    renderList();
    toast(action === "take" ? "Диалог у вас. Бот в этом чате молчит." : "Диалог возвращён боту.");
  } catch (error) { fail(error); }
}

$("#btn-take").addEventListener("click", () => convAction("take"));
$("#btn-release").addEventListener("click", () => convAction("release"));

const reply = $("#reply");
let replyFaq = null;

function autosize() {
  reply.style.height = "auto";
  reply.style.height = Math.min(reply.scrollHeight, 180) + "px";
  $("#btn-send").disabled = !reply.value.trim();
}

reply.addEventListener("input", () => { autosize(); if (!reply.value.trim()) replyFaq = null; });
reply.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendReply(); }
});
$("#btn-send").addEventListener("click", sendReply);

async function sendReply() {
  const text = reply.value.trim();
  if (!text || !S.open) return;
  const chatId = S.open;
  const propose = $("#propose").checked;
  $("#btn-send").disabled = true;
  try {
    const result = await api("POST", `/api/conversations/${encodeURIComponent(chatId)}/send`, { text, propose, faq_id: replyFaq });
    reply.value = "";
    replyFaq = null;
    $("#propose").checked = false;
    autosize();
    if (!result.delivered) toast("Сообщение сохранено, но не доставлено", "bad", "Бот не подключён к Telegram.");
    else if (result.proposal_id) toast("Ответ отправлен и предложен в базу", "ok", "Его увидит старший в разделе «Обучение».");
    await loadMessages(chatId, true);
  } catch (error) { fail(error); }
  finally { autosize(); }
}

/* ---------------- всплывающие списки ---------------- */

const popover = $("#popover");
let popoverOwner = null;

function closePopover() { popover.hidden = true; popoverOwner = null; }

function placePopover(anchor) {
  popover.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  let left = Math.min(Math.max(12, rect.left), innerWidth - width - 12);
  let top = rect.bottom + 6;
  if (top + height > innerHeight - 12) top = Math.max(12, rect.top - height - 6);
  popover.style.left = left + "px";
  popover.style.top = top + "px";
}

document.addEventListener("mousedown", (event) => {
  if (!popover.hidden && !popover.contains(event.target) && event.target.closest("[data-teach],#btn-faq,.q-more") !== popoverOwner) closePopover();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePopover();
  if (event.key === "/" && document.activeElement === document.body && S.view === "chats") { event.preventDefault(); $("#search").focus(); }
});

function pickerList(items, onPick) {
  const list = h("div", { class: "pop-list" });
  const input = h("input", { type: "search", placeholder: "Поиск темы", autocomplete: "off" });
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    const nodes = [];
    for (const item of items) {
      if (item.sep) { if (!q) nodes.push(h("div", { class: "pop-sep", text: item.sep })); continue; }
      if (q && !item.search.includes(q)) continue;
      nodes.push(h("button", { class: "pop-item " + (item.cls || ""), onclick: () => onPick(item) },
        h("span", { class: "num", text: item.num }), h("span", null, item.title, item.sub ? h("small", { text: item.sub }) : null)));
    }
    list.replaceChildren(...(nodes.length ? nodes : [h("div", { class: "empty", text: "Ничего не нашлось" })]));
  };
  input.addEventListener("input", draw);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { const first = list.querySelector(".pop-item"); if (first) first.click(); }
  });
  draw();
  return { input, list };
}

function openTopicPicker(anchor, messageId, suggest) {
  if (popoverOwner === anchor) { closePopover(); return; }
  const items = [];
  if (suggest.length) {
    items.push({ sep: "Похожие темы" });
    for (const id of suggest) items.push(topicItem(id, "suggested"));
    items.push({ sep: "Все темы" });
  }
  for (const id of S.faq.keys()) if (!suggest.includes(id)) items.push(topicItem(id));
  items.push({ num: "—", title: "Вне базы", sub: "Бот этому не учится", search: "вне базы не по теме", cls: "off", id: null });
  const { input, list } = pickerList(items, (item) => { closePopover(); tagMessage(messageId, item.id); });
  popover.replaceChildren(h("div", { class: "pop-head" }, h("b", { text: "К какой теме относится вопрос?" }),
    h("label", { class: "search" }, icon("search"), input)), list);
  popoverOwner = anchor;
  placePopover(anchor);
  input.focus();
}

function topicItem(id, cls = "") {
  const entry = S.faq.get(id);
  return { id, num: id, title: entry.question_ru, sub: entry.question_ky, cls,
    search: (entry.question_ru + " " + entry.question_ky + " " + id).toLowerCase() };
}

async function tagMessage(messageId, faqId) {
  try {
    const verdict = await api("POST", `/api/messages/${messageId}/tag`, { faq_id: faqId });
    if (verdict.code === "offtopic") toast(verdict.message);
    else if (verdict.accepted) {
      const detail = verdict.after && verdict.after.total
        ? `Открытый набор: top-1 ${percent(verdict.before.top1_share)} → ${percent(verdict.after.top1_share)}, top-3 ${percent(verdict.before.top3_share)} → ${percent(verdict.after.top3_share)}`
        : "";
      toast(verdict.message, "ok", detail);
    } else toast("Не принято", "bad", verdict.message);
    if (S.open) loadMessages(S.open, false);
    if (S.view === "learning") loadLearning();
  } catch (error) { fail(error); }
}

$("#btn-faq").addEventListener("click", (event) => {
  const anchor = event.currentTarget;
  if (popoverOwner === anchor) { closePopover(); return; }
  const conv = S.convs.get(S.open);
  const lang = conv ? conv.lang : "ru";
  const items = [...S.faq.values()].map((entry) => ({
    id: entry.id, num: entry.id, title: lang === "ky" ? entry.question_ky : entry.question_ru,
    sub: lang === "ky" ? entry.answer_ky : entry.answer_ru,
    search: (entry.question_ru + " " + entry.question_ky + " " + entry.answer_ru + " " + entry.answer_ky).toLowerCase(),
  }));
  const { input, list } = pickerList(items, (item) => {
    closePopover();
    insertFaq(item.id, lang);
  });
  popover.replaceChildren(h("div", { class: "pop-head" }, h("b", { text: `Ответ из базы (${LANG[lang]}) — факты только из faq.json` }),
    h("label", { class: "search" }, icon("search"), input)), list);
  popoverOwner = anchor;
  placePopover(anchor);
  input.focus();
});

function insertFaq(id, lang) {
  const entry = S.faq.get(id);
  if (!entry) return;
  const text = lang === "ky" ? entry.answer_ky : entry.answer_ru;
  reply.value = reply.value.trim() ? reply.value.trimEnd() + "\n" + text : text;
  replyFaq = id;
  autosize();
  reply.focus();
}

/* ---------------- карточка абонента ---------------- */

$("#btn-info").addEventListener("click", () => {
  const pane = $("#info-pane");
  pane.hidden = !pane.hidden;
  $("#btn-info").classList.toggle("active", !pane.hidden);
  store.set("info", !pane.hidden);
  if (!pane.hidden) renderInfo();
});

function renderInfo() {
  const conv = S.convs.get(S.open);
  const pane = $("#info-pane");
  if (!conv) { pane.replaceChildren(); return; }
  const list = S.msgs.get(S.open) || [];
  const incoming = list.filter((m) => m.dir === "in").length;
  const topics = [...new Set(list.filter((m) => m.faq_id).map((m) => m.faq_id))];
  const kv = (rows) => h("dl", { class: "kv" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
  pane.replaceChildren(
    h("div", { class: "info-block" }, h("h4", { text: "Абонент" }), kv([
      ["Имя", displayName(conv)],
      ["Язык", conv.lang === "ky" ? "Кыргызский" : "Русский"],
      ["Канал", conv.source === "demo" ? "Демо" : "Telegram"],
      ["Первое обращение", new Date(conv.created_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })],
      ["Реплик", String(incoming)],
      ["Бот не понял", String(conv.bot_misses)],
    ])),
    h("div", { class: "info-block" }, h("h4", { text: "Темы в этом диалоге" }),
      topics.length ? h("div", { class: "faq-mini" }, topics.map((id) => h("button", { onclick: () => insertFaq(id, conv.lang) }, h("b", { text: id }), topicName(id, conv.lang))))
        : h("div", { class: "muted", text: "Бот ещё не нашёл ни одной темы." })),
    h("div", { class: "info-block" }, h("h4", { text: "Быстрые ответы" }),
      h("div", { class: "faq-mini" }, [...S.faq.values()].slice(0, 15).map((entry) => h("button", { onclick: () => insertFaq(entry.id, conv.lang) }, h("b", { text: entry.id }), conv.lang === "ky" ? entry.question_ky : entry.question_ru)))),
  );
}

/* ---------------- обучение ---------------- */

async function loadLearning() {
  let data;
  try { data = await api("GET", "/api/learning"); } catch (error) { if (error.status !== 401) fail(error); return; }
  const queue = $("#queue");
  $("#queue-count").textContent = data.queue.length;
  const qb = $("#badge-queue");
  qb.hidden = !data.queue.length;
  qb.textContent = data.queue.length;
  queue.replaceChildren(...(data.queue.length ? data.queue.map((item) => {
    const conv = S.convs.get(item.chat_id) || { chat_id: item.chat_id, title: item.title };
    const actions = h("div", { class: "q-actions" },
      (item.suggest || []).map((id) => h("button", { class: "chip", title: topicName(id), onclick: (event) => { event.currentTarget.closest(".q-item").classList.add("leaving"); tagMessage(item.id, id); } }, h("b", { text: id }), shorten(topicName(id), 34))),
      h("button", { class: "chip q-more", onclick: (event) => openTopicPicker(event.currentTarget, item.id, item.suggest || []) }, "Другая тема…"),
      h("button", { class: "chip", onclick: () => tagMessage(item.id, null) }, "Вне базы"),
      h("button", { class: "chip", onclick: () => openChat(item.chat_id) }, "Открыть диалог"));
    return h("div", { class: "q-item" },
      h("div", { class: "q-text", text: item.text }),
      h("div", { class: "q-meta" }, h("span", { class: "lang " + (item.chat_lang || "ru"), text: LANG[item.chat_lang] || "RU" }), displayName(conv), "·", shortTime(item.created_at),
        item.confidence != null ? h("span", { text: "· уверенность бота " + percent(item.confidence) }) : null),
      actions);
  }) : [h("div", { class: "empty" }, h("b", { text: "Очередь пуста" }), "Все реплики, которые бот не понял, уже размечены.")]));

  $("#learned-count").textContent = data.learned.length;
  $("#learned").replaceChildren(...(data.learned.length ? data.learned.map((row) => {
    let report = {};
    try { report = JSON.parse(row.report || "{}"); } catch { /* старый формат */ }
    return h("div", { class: "l-item" },
      h("div", { class: "l-row" }, h("span", { class: "l-phrase", text: "«" + row.phrase + "»" }),
        h("button", { class: "btn ghost sm", onclick: () => disableLearned(row.id) }, "Отменить")),
      h("div", { class: "l-meta" }, h("span", { class: "tagchip topic", text: "Тема " + row.faq_id }), " ", shorten(topicName(row.faq_id), 40),
        ` · ${row.operator} · ${shortTime(row.created_at)}`,
        report.after && report.after.total ? ` · top-1 ${percent(report.after.top1_share)}` : ""));
  }) : [h("div", { class: "empty" }, h("b", { text: "Пока ничего" }), "Отметьте тему у непонятой реплики — бот запомнит формулировку.")]));

  $("#proposals-count").textContent = data.proposals.filter((p) => p.status === "pending").length;
  $("#proposals").replaceChildren(...(data.proposals.length ? data.proposals.map((row) => h("div", { class: "p-item" },
    h("div", { class: "l-row" }, h("span", { class: "p-q", text: "Вопрос: " + (row.question || "—") }),
      h("span", { class: "status-tag " + row.status, text: { pending: "на проверке", approved: "одобрено", rejected: "отклонено" }[row.status] })),
    h("div", { class: "p-a", text: row.answer }),
    h("div", { class: "l-row" }, h("span", { class: "l-meta", text: `${row.operator} · ${LANG[row.lang] || row.lang}` + (row.faq_id ? ` · тема ${row.faq_id}` : "") + (row.reviewer ? ` · проверил(а) ${row.reviewer}` : "") }),
      row.status === "pending" ? h("span", { class: "q-actions" },
        h("button", { class: "btn ok sm", onclick: () => review(row.id, "approved") }, "Одобрить"),
        h("button", { class: "btn danger sm", onclick: () => review(row.id, "rejected") }, "Отклонить")) : null),
  )) : [h("div", { class: "empty" }, h("b", { text: "Предложений нет" }), "Поставьте галочку под ответом оператора — он придёт сюда на проверку.")]));
}

function shorten(text, n) { text = String(text || ""); return text.length > n ? text.slice(0, n - 1) + "…" : text; }

async function disableLearned(id) {
  try { await api("POST", `/api/learned/${id}/disable`, {}); toast("Фраза отменена, бот её больше не использует."); loadLearning(); }
  catch (error) { fail(error); }
}

async function review(id, status) {
  try { await api("POST", `/api/proposals/${id}/review`, { status }); loadLearning(); }
  catch (error) { fail(error); }
}

/* ---------------- аналитика ---------------- */

async function loadAnalytics() {
  let a;
  try { a = await api("GET", "/api/analytics"); } catch (error) { if (error.status !== 401) fail(error); return; }
  const handled = a.bot_answered + a.bot_missed;
  const share = handled ? a.bot_answered / handled : 0;
  const kpi = (label, value, small, cls = "") => h("div", { class: "kpi " + cls }, h("span", { text: label }), h("b", { text: value }), small ? h("small", { text: small }) : null);
  $("#kpis").replaceChildren(
    kpi("Ждут сейчас", String(a.waiting), a.oldest_wait ? "дольше всех — " + duration(a.oldest_wait) : "очередь пуста", a.waiting ? "bad" : "ok"),
    kpi("У операторов", String(a.with_operator), `диалогов всего: ${a.conversations}`),
    kpi("Бот ответил сам", handled ? percent(share) : "—", `${a.bot_answered} из ${handled} за сутки`, share >= 0.8 ? "ok" : ""),
    kpi("Медиана ответа оператора", a.median_wait != null ? duration(a.median_wait) : "—", a.p90_wait != null ? "90% — быстрее " + duration(a.p90_wait) : "за 7 дней"),
    kpi("Реплик за сутки", String(a.messages_today), `активных диалогов: ${a.active_today}`),
    kpi("Бот выучил", String(a.learned), `в очереди: ${a.queue} · предложений: ${a.proposals_pending}`),
  );

  const max = Math.max(1, ...a.hours);
  const startHour = new Date((a.now - 86400) * 1000).getHours();
  $("#chart-hours").replaceChildren(...a.hours.map((n, index) => {
    const bar = h("div", { class: "hour" + (n ? "" : " zero"), title: `${n} реплик` },
      h("em", { text: String(n) }), h("i", { text: String((startHour + index + 1) % 24).padStart(2, "0") }));
    bar.style.height = Math.max(2, (n / max) * 100) + "%";
    return bar;
  }));

  const bars = (rows, label, cls = "") => {
    if (!rows.length) return [h("div", { class: "empty", text: "Пока нет данных" })];
    const top = Math.max(...rows.map((r) => r.n));
    return rows.slice(0, 8).map((row) => {
      const fill = h("div", { class: "bar-fill " + cls });
      fill.style.width = (row.n / top) * 100 + "%";
      return h("div", { class: "bar" }, h("span", { text: label(row), title: label(row) }), h("b", { text: String(row.n) }), h("div", { class: "bar-track" }, fill));
    });
  };
  $("#chart-topics").replaceChildren(...bars(a.topics, (r) => `${r.faq_id}. ${topicName(r.faq_id)}`, "bot"));
  $("#chart-reasons").replaceChildren(...bars(a.reasons, (r) => REASONS[r.reason] || r.reason, "bad"));

  const total = a.languages.reduce((sum, r) => sum + r.n, 0);
  const track = h("div", { class: "split-track" });
  const legend = h("div", { class: "split-legend" });
  for (const row of a.languages) {
    const part = h("i", { class: "c-" + row.lang });
    part.style.width = (total ? (row.n / total) * 100 : 0) + "%";
    track.append(part);
    legend.append(h("span", null, h("i", { class: "c-" + row.lang }), `${row.lang === "ky" ? "Кыргызский" : "Русский"} — ${row.n} (${percent(total ? row.n / total : 0)})`));
  }
  $("#chart-langs").replaceChildren(...(total ? [track, legend] : [h("div", { class: "empty", text: "Пока нет данных" })]));

  $("#chart-ops").replaceChildren(...(a.operators.length ? a.operators.map((row) => {
    const av = h("span", { class: "avatar", text: initials(row.name) });
    av.style.setProperty("--h", hue(row.name));
    return h("div", { class: "op-row" }, av, h("span", { text: row.name }), h("b", { text: row.n + " отв." }),
      h("span", { class: "muted", text: row.avg_wait != null ? "≈ " + duration(row.avg_wait) : "" }));
  }) : [h("div", { class: "empty", text: "Операторы ещё не отвечали" })]));
}

/* ---------------- демо-клиент ---------------- */

const DEMO_HINTS = ["Скока денег на счету?", "Balansymdy kantip korom", "Интернетим иштебей жатат", "Как подключить роуминг?", "А сколько стоит?", "Курс доллара", "Оператор"];

async function openDemo(fresh) {
  try {
    const current = S.open && S.convs.get(S.open);
    if (!fresh && current && current.source === "demo") S.demoChat = current.chat_id;
    else if (fresh || !S.demoChat || !S.convs.has(S.demoChat)) {
      const data = await api("POST", "/api/demo/new", {});
      S.convs.set(data.conversation.chat_id, data.conversation);
      S.demoChat = data.conversation.chat_id;
      renderList();
    }
    $("#demo").hidden = false;
    $("#demo-name").textContent = displayName(S.convs.get(S.demoChat));
    $("#demo-hints").replaceChildren(...DEMO_HINTS.map((text) => h("button", { onclick: () => demoSend(text) }, text)));
    if (S.open !== S.demoChat) openChat(S.demoChat);
    await renderDemo();
    $("#demo-text").focus();
  } catch (error) { fail(error); }
}

$("#demo-open").addEventListener("click", () => ($("#demo").hidden ? openDemo(false) : ($("#demo").hidden = true)));
$("#empty-demo").addEventListener("click", () => openDemo(true));
$("#demo-new").addEventListener("click", () => openDemo(true));
$("#demo-close").addEventListener("click", () => { $("#demo").hidden = true; });
$("#demo-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#demo-text");
  const text = input.value.trim();
  if (text) { input.value = ""; demoSend(text); }
});

async function demoSend(text) {
  if (!S.demoChat) return;
  try { await api("POST", `/api/demo/${encodeURIComponent(S.demoChat)}/message`, { text }); }
  catch (error) { fail(error); }
}

async function demoAction(action) {
  try { await api("POST", `/api/demo/${encodeURIComponent(S.demoChat)}/action`, { action }); }
  catch (error) { fail(error); }
}

async function renderDemo() {
  if ($("#demo").hidden || !S.demoChat) return;
  const chatId = S.demoChat;
  let data;
  try { data = await api("GET", `/api/conversations/${encodeURIComponent(chatId)}/messages?limit=60`); }
  catch { return; }
  if (chatId !== S.demoChat) return;
  const screen = $("#demo-screen");
  const nodes = [];
  const last = data.messages.length - 1;
  data.messages.forEach((m, index) => {
    if (m.dir === "sys") {
      if (m.tag && (m.tag.startsWith("handoff") || m.tag === "take" || m.tag === "release")) nodes.push(h("div", { class: "d-sys", text: m.text }));
      return;
    }
    if (m.dir === "in") { nodes.push(h("div", { class: "d-msg me", text: m.text })); return; }
    nodes.push(h("div", { class: "d-msg them" + (m.dir === "op" ? " op" : "") }, h("small", { text: m.dir === "op" ? "🧑‍💼 " + (m.author || "Оператор") : "INAI Mobile" }), m.text));
    if (m.dir === "bot" && m.buttons && m.buttons.length && index === last) {
      nodes.push(h("div", { class: "d-btns" }, m.buttons.map(([label, action]) => h("button", { onclick: () => demoAction(action) }, label))));
    }
  });
  if (!nodes.length) nodes.push(h("div", { class: "d-sys", text: "Напишите боту — как абонент в Telegram." }));
  screen.replaceChildren(...nodes);
  screen.scrollTop = screen.scrollHeight;
}

/* ---------------- старт ---------------- */

applyTheme(store.get("theme", null));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store.get("theme", null)));
if (store.get("info", false)) { $("#info-pane").hidden = false; $("#btn-info").classList.add("active"); }
autosize();

api("GET", "/api/me").then((me) => (me && me.name ? start() : showLogin())).catch(showLogin);
