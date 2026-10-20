// Newtonite Ops frontend. Plain JS modules, no build step.
// Server state is fetched per page; the URL holds filter state; every
// mutation carries an Idempotency-Key so a retry can never act twice; live
// updates arrive by server push and only ever *offer* a refresh.

import { changedFields, conflictRows } from "./edits.js";
import { safeNext } from "./redirect.js";
import { latest } from "./state.js";

// ---------------------------------------------------------------- API ----

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details || null;
  }
}

export async function api(method, path, body = null, { idempotencyKey } = {}) {
  const headers = {};
  if (body !== null) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let res;
  try {
    res = await fetch(path, {
      method, headers, credentials: "same-origin",
      body: body !== null ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Could not reach the server. Check your connection and try again.");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    const e = (data && data.error) || {};
    if (res.status === 401 && path !== "/api/login") {
      location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.search);
    }
    throw new ApiError(res.status, e.code || `HTTP_${res.status}`, e.message || res.statusText, e.details);
  }
  return data;
}

export const newKey = () => crypto.randomUUID();

// One key per user intent: reused when the user retries after a network
// error (so the server can recognise the repeat), dropped after any answer.
function intent() {
  let key = null;
  return {
    key: () => (key ??= newKey()),
    settle: (err) => { if (!(err instanceof ApiError && err.code === "NETWORK")) key = null; },
  };
}

// Disable a button while its request is in flight (no double submits).
async function busy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try { return await fn(); } finally { button.disabled = false; }
}

// ------------------------------------------------------------ helpers ----

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const $ = (sel) => document.querySelector(sel);
const label = (s) => {
  const t = (s || "").replaceAll("_", " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
const STATUSES = ["awaiting_approval", "open", "in_progress", "blocked", "done", "cancelled"];
const PRIORITIES = ["urgent", "high", "medium", "low"];
const TYPES = ["customer_issue", "engineering", "payment", "incident", "compliance", "task"];
const TERMINAL = new Set(["done", "cancelled"]);
// Same table as app/rules.py: status -> action -> new status.
const TRANSITIONS = {
  awaiting_approval: { approve: "open", reject: "cancelled" },
  open: { start: "in_progress", cancel: "cancelled" },
  in_progress: { block: "blocked", complete: "done", cancel: "cancelled" },
  blocked: { unblock: "in_progress", cancel: "cancelled" },
  done: { reopen: "in_progress" },
  cancelled: {},
};
const ACTION_LABEL = {
  approve: "Approve", reject: "Reject", start: "Start work", block: "Block",
  unblock: "Unblock", complete: "Complete", reopen: "Reopen", cancel: "Cancel",
};
const REASON_REQUIRED = new Set(["reject", "block", "complete", "cancel"]);
const PAST = {
  approve: "approved", reject: "rejected", start: "started work", block: "blocked",
  unblock: "unblocked", complete: "completed", reopen: "reopened", cancel: "cancelled",
};

function ago(iso) {
  if (!iso) return "";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "None");
const statusBadge = (s) => el("span", { class: `badge st st-${s}` }, el("span", { class: "status-dot" }), label(s));
const prioBadge = (p) => el("span", { class: `badge pr pr-${p}` }, label(p));

function avatar(person) {
  if (!person) return el("span", { class: "avatar none", title: "Unassigned" }, "?");
  const initials = person.name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  let h = 0;
  for (const ch of String(person.id)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return el("span", { class: "avatar", style: `--h:${h}`, title: person.name }, initials);
}

// "29 Oct" (plus the year when it isn't this year).
function shortDate(iso) {
  const d = new Date(iso);
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

// `whenFinished: false` hides the chip on done/cancelled tasks, where a due date no longer matters.
function dueChip(item, { whenFinished = true } = {}) {
  if (!item.due_at) return null;
  if (!whenFinished && TERMINAL.has(item.status)) return null;
  const ms = new Date(item.due_at).getTime() - Date.now();
  const open = !TERMINAL.has(item.status);
  const cls = open && ms < 0 ? "due late" : open && ms < 48 * 3600e3 ? "due soon" : "due";
  const days = Math.floor(-ms / 86400e3);
  const text = open && ms < 0 ? (days >= 1 ? `Overdue ${days}d` : "Overdue today") : `Due ${shortDate(item.due_at)}`;
  return el("span", { class: cls, title: fmtDate(item.due_at) }, text);
}

function icon(d) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("fill", "currentColor");
  path.setAttribute("d", d);
  svg.append(path);
  return svg;
}
const REFRESH_ICON = "M13.6 2.4v4.2H9.4l1.6-1.6A4.6 4.6 0 1 0 12.4 9h1.5a6 6 0 1 1-1.8-5.1l1.5-1.5z";

// Small padlock tag for tasks whose details only leads and the creator may edit.
function lockTag(text = "Locked") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("fill", "currentColor");
  path.setAttribute("d", "M5 7V5a3 3 0 0 1 6 0v2h1a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h1zm1.5 0h3V5a1.5 1.5 0 0 0-3 0v2z");
  svg.append(path);
  return el("span", { class: "lock", title: "Only team leads and the creator can edit the details" }, svg, text);
}

function fillSelect(select, values) {
  for (const v of values) select.append(el("option", { value: v }, label(v)));
}

function showError(node, err) {
  node.textContent = err ? err.message : "";
  node.hidden = !err;
}

let flashTimer;
function flash(message, ok = false) {
  const box = $("#flash");
  if (!box) return;
  box.textContent = message;
  box.className = ok ? "flash ok" : "flash";
  box.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

// A message that survives one navigation (e.g. "moved to another team").
function flashNext(message) { try { sessionStorage.setItem("flash", message); } catch { /* storage off */ } }
function flashPending() {
  try {
    const m = sessionStorage.getItem("flash");
    if (m) { sessionStorage.removeItem("flash"); flash(m, true); }
  } catch { /* storage off */ }
}

// ------------------------------------------------------------ top bar ----

let refreshHandler = () => location.reload();

async function topbar(active) {
  const me = await api("GET", "/api/me");
  const tab = (href, text, key) => el("a", { href, "aria-current": active === key ? "page" : null }, text);
  $("#topbar").replaceChildren(
    el("a", { class: "brand", href: "/" }, "Newtonite Ops"),
    el("nav", { class: "tabs", "aria-label": "Views" }, tab("/", "List", "list"), tab("/board.html", "Board", "board")),
    el("span", { class: "spacer" }),
    el("span", { class: "live", id: "live-state", title: "Whether this page hears about changes as they happen" }, "Connecting"),
    el("button", { type: "button", id: "refresh-btn", class: "with-icon", title: "Load the latest data",
      onclick: () => refreshHandler() }, icon(REFRESH_ICON), "Refresh"),
    el("a", { href: "/new.html" }, el("button", { type: "button", class: "primary", tabindex: "-1" }, "New task")),
    el("span", { class: "me" }, avatar(me.user), el("span", { class: "me-name" }, me.user.name)),
    el("button", { type: "button", onclick: async () => { await api("POST", "/api/logout"); location.href = "/login.html"; } }, "Log out"),
  );
  return me;
}

// ------------------------------------------------------- live updates ----
// The server pushes "item X changed" notices (SSE). We never redraw on our
// own: we count changes made by other people and offer a Refresh.

function startLive(me, { relevant, describe, onRefresh }) {
  const banner = $("#live-banner");
  const state = $("#live-state");
  const pending = new Set(); // ids of tasks others changed (one task moved 4 times = 1)
  let missed = false;
  let hadError = false;

  const show = () => {
    if (!pending.size && !missed) { banner.hidden = true; return; }
    const text = missed
      ? "The live connection dropped for a moment, so you may have missed changes."
      : describe(pending.size);
    banner.replaceChildren(el("span", {}, text),
      el("button", { type: "button", class: "primary", onclick: () => refreshHandler() }, "Refresh"));
    banner.hidden = false;
  };
  refreshHandler = async () => {
    pending.clear(); missed = false; show();
    await onRefresh();
  };

  const source = new EventSource("/api/events");
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.type === "hello") {
      state.className = "live on"; state.textContent = "Live";
      if (hadError) { missed = true; show(); } // reconnected: we can't know what we missed
      hadError = false;
    } else if (ev.type === "resync") {
      missed = true; show();
    } else if (ev.type === "change" && ev.actor_id !== me.user.id && relevant(ev)) {
      pending.add(ev.item_id); show();
    }
  };
  source.onerror = () => {
    hadError = true;
    state.className = "live off"; state.textContent = "Reconnecting";
  };
}

// ------------------------------------------------------ status changes ----

function nextStatuses(item) {
  return Object.entries(TRANSITIONS[item.status] || {})
    .filter(([action]) => item.allowed_actions.includes(action));
}

const transitionIntents = new Map();
// Ask for a reason when needed, send the transition, return the updated item (or null).
async function runTransition(item, action) {
  let reason = null;
  if (REASON_REQUIRED.has(action)) {
    reason = window.prompt(`${ACTION_LABEL[action]} "${item.title}": why? (required)`);
    if (reason === null) return null;
    if (!reason.trim()) { flash("A reason is required for this change."); return null; }
  }
  const k = `${item.id}:${action}:${item.version}`;
  const it = transitionIntents.get(k) ?? intent();
  transitionIntents.set(k, it);
  try {
    const updated = await api("POST", `/api/items/${item.id}/transitions`,
      { action, reason, expected_version: item.version }, { idempotencyKey: it.key() });
    it.settle(null);
    transitionIntents.delete(k);
    flash(`"${updated.title}" ${PAST[action]}.`, true);
    return updated;
  } catch (err) {
    it.settle(err);
    flash(err.code === "STALE_VERSION"
      ? `"${item.title}" was changed by someone else. Refresh to see the latest, then try again.`
      : err.message);
    return null;
  }
}

// The status as a dropdown offering only the moves this user may make.
function statusSelect(item, onUpdated) {
  const moves = nextStatuses(item);
  if (moves.length === 0) {
    const badge = statusBadge(item.status);
    badge.classList.add("status-pill");
    badge.title = "You can't change this task's status";
    return badge;
  }
  const select = el("select", { class: `status-select status-pill st st-${item.status}`, "aria-label": `Change status of ${item.title}` },
    el("option", { value: "", selected: true }, label(item.status)),
    moves.map(([action, to]) => el("option", { value: action, title: `Moves it to ${label(to)}` }, `→ ${ACTION_LABEL[action]}`)));
  select.addEventListener("change", async () => {
    const action = select.value;
    if (!action) return;
    select.disabled = true;
    const updated = await runTransition(item, action);
    select.disabled = false;
    select.value = "";
    if (updated) onUpdated(updated);
  });
  return select;
}

// ------------------------------------------------------- login/signup ----

async function initLogin() {
  const form = $("#login-form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    await busy(form.querySelector("button"), async () => {
      try {
        await api("POST", "/api/login", data);
        location.href = safeNext(new URLSearchParams(location.search).get("next"), location.origin);
      } catch (err) {
        showError($("#login-error"), err);
      }
    });
  });
}

async function initSignup() {
  const form = $("#signup-form");
  const f = form.elements;
  const teams = await api("GET", "/api/teams");
  for (const t of teams) f.team_id.append(el("option", { value: t.id }, t.name));
  if (teams.length === 0) {
    // First person in: there is nothing to join yet.
    $("#join-option").hidden = true;
    f.team_id.hidden = true;
    f.team_mode.value = "create";
  }
  const syncMode = () => {
    const create = f.team_mode.value === "create";
    f.team_id.disabled = create;
    f.new_team_name.disabled = !create;
    f.new_team_name.required = create;
  };
  form.addEventListener("change", syncMode);
  syncMode();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const create = f.team_mode.value === "create";
    const body = {
      name: f.name.value, email: f.email.value, password: f.password.value,
      ...(create ? { new_team_name: f.new_team_name.value } : { team_id: f.team_id.value }),
    };
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        await api("POST", "/api/signup", body);
        location.href = "/";
      } catch (err) {
        const msg = err.code === "VALIDATION_FAILED"
          ? "Check the form: a valid email, a password of at least 8 characters, and a team."
          : err.message;
        showError($("#signup-error"), new Error(msg));
      }
    });
  });
}

// ------------------------------------------------------- list (overview) ----

// Tile accent colours follow what each view is about.
const VIEW_COLORS = {
  mine: "var(--s-in_progress)", unassigned_urgent: "var(--p-urgent)", awaiting_my_approval: "var(--s-awaiting_approval)",
  overdue: "var(--s-blocked)", stale: "var(--p-high)", requested_by_me: "var(--s-open)",
};
const VIEW_LABELS = {
  mine: "Assigned to me",
  unassigned_urgent: "Unassigned urgent or high",
  awaiting_my_approval: "Awaiting my approval",
  overdue: "Overdue",
  stale: "No activity for 3+ days",
  requested_by_me: "Requested by me",
};

function titleCell(it) {
  return el("td", { class: "title" },
    el("a", { href: `/item.html?id=${it.id}` }, it.title),
    it.restricted ? [" ", lockTag()] : null,
    it.my_role === "requester" ? [" ", el("span", { class: "tag" }, "Your request")] : null);
}

async function initOverview() {
  const me = await topbar("list");
  flashPending();
  const form = $("#filters");
  for (const t of me.teams) form.elements.team_id.append(el("option", { value: t.id }, t.name));
  fillSelect(form.elements.status, STATUSES);
  fillSelect(form.elements.priority, PRIORITIES);

  const FIELDS = ["q", "team_id", "status", "priority", "assignee", "sort"];
  const params = () => new URLSearchParams(location.search);
  let requestSeq = 0; // ignore responses that arrive after a newer request

  function setParams(changes, { push = true } = {}) {
    const p = params();
    for (const [k, v] of Object.entries(changes)) (v ? p.set(k, v) : p.delete(k));
    if (!("page" in changes)) p.delete("page");
    const url = `${location.pathname}${p.toString() ? "?" + p : ""}`;
    push ? history.pushState(null, "", url) : history.replaceState(null, "", url);
    load();
  }

  function syncForm() {
    const p = params();
    for (const f of FIELDS) {
      // Don't rewrite the search box while the user is typing in it (it would eat spaces).
      if (f === "q" && document.activeElement === form.elements.q) continue;
      form.elements[f].value = p.get(f) || "";
    }
  }

  const hour = new Date().getHours();
  const firstName = me.user.name.split(/\s+/)[0];
  $("#greeting").textContent = `${hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"}, ${firstName}`;

  // One sentence about what needs this person, built from the same counts as the tiles.
  function summary(c) {
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    const parts = [];
    if (c.mine) parts.push(plural(c.mine, "task assigned to you", "tasks assigned to you"));
    if (c.overdue) parts.push(`${c.overdue} overdue`);
    if (c.awaiting_my_approval) parts.push(`${c.awaiting_my_approval} waiting for your approval`);
    if (c.unassigned_urgent) parts.push(`${c.unassigned_urgent} urgent with no owner`);
    const join = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);
    if (!parts.length) {
      return c.stale ? `You're all caught up. ${plural(c.stale, "task has", "tasks have")} had no activity for 3+ days.`
        : "You're all caught up. Nothing needs you right now.";
    }
    return `Here's what needs you: ${join(parts)}.`;
  }

  async function loadTiles() {
    const { counts } = await api("GET", "/api/overview");
    const active = params().get("view");
    $("#summary").textContent = summary(counts);
    // Nothing to show anywhere: the summary line says so; skip six empty tiles.
    const anything = Object.values(counts).some(Boolean);
    $("#tiles").hidden = !anything && !active;
    $("#tiles").replaceChildren(...Object.entries(VIEW_LABELS).map(([view, text]) =>
      el("button", {
        type: "button", style: `--c: ${VIEW_COLORS[view]}`,
        class: `tile ${counts[view] ? "has-work" : "zero"}${active === view ? " active" : ""}`, "aria-pressed": String(active === view),
        title: active === view ? "Show all tasks again" : `Show only: ${text}`,
        onclick: () => setParams({ view: active === view ? "" : view }),
      }, el("span", { class: "count" }, counts[view]), el("span", { class: "tile-label" }, text))));
  }

  let showTeam = me.teams.length > 1;

  function head() {
    const cols = ["Title", showTeam ? "Team" : null, "Status", "Priority", "Assignee", "Due", "Last activity"];
    $("#head-row").replaceChildren(...cols.filter(Boolean).map((c) => el("th", { scope: "col" }, c)));
  }

  function row(it) {
    const href = `/item.html?id=${it.id}`;
    const tr = el("tr", { class: "clickable" },
      titleCell(it),
      showTeam ? el("td", {}, it.team.name) : null,
      el("td", {}, statusSelect(it, (updated) => { tr.replaceWith(row({ ...it, ...updated })); loadTiles().catch(() => {}); })),
      el("td", {}, prioBadge(it.priority)),
      el("td", {}, el("span", { class: "who" }, avatar(it.assignee), it.assignee ? it.assignee.name : el("span", { class: "muted" }, "Unassigned"))),
      el("td", {}, dueChip(it, { whenFinished: false }) || el("span", { class: "muted" }, "–")),
      el("td", { title: fmtDate(it.last_activity_at), class: "muted" }, ago(it.last_activity_at)),
    );
    // The whole row opens the task, except its own controls.
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("a, select, button, input")) return;
      location.href = href;
    });
    return tr;
  }

  async function load() {
    syncForm();
    const p = params();
    const page = Number(p.get("page") || 1);
    const view = p.get("view");
    $("#active-view").hidden = !view;
    $("#active-view").textContent = view ? `Showing: ${VIEW_LABELS[view] || view}` : "";
    const seq = ++requestSeq;
    loadTiles().catch(() => {});
    try {
      const data = await api("GET", `/api/items?${p}`);
      if (seq !== requestSeq) return;
      showError($("#list-error"), null);
      // Show the Team column when it tells you something.
      showTeam = me.teams.length > 1 || data.items.some((i) => i.my_role === "requester");
      head();
      $("#rows").replaceChildren(...data.items.map(row));
      $("#empty").hidden = data.items.length > 0;
      const n = data.items.length;
      const first = (page - 1) * data.page_size + 1;
      $("#result-count").textContent = n === 0 ? ""
        : page === 1 && !data.has_more ? `${n} ${n === 1 ? "task" : "tasks"}`
          : `Tasks ${first}–${first + n - 1}`;
      $("#pager").hidden = page === 1 && !data.has_more;
      $("#prev").disabled = page <= 1;
      $("#next").disabled = !data.has_more;
      $("#page-label").textContent = `Page ${page}`;
    } catch (err) {
      if (seq === requestSeq) showError($("#list-error"), err);
    }
  }

  let typing;
  form.elements.q.addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => setParams({ q: form.elements.q.value.trim() }, { push: false }), 300);
  });
  form.addEventListener("submit", (ev) => ev.preventDefault());
  for (const f of ["team_id", "status", "priority", "assignee", "sort"]) {
    form.elements[f].addEventListener("change", () => setParams({ [f]: form.elements[f].value }));
  }
  $("#clear-filters").addEventListener("click", () => { history.pushState(null, "", location.pathname); load(); });
  $("#prev").addEventListener("click", () => setParams({ page: String(Number(params().get("page") || 1) - 1) }));
  $("#next").addEventListener("click", () => setParams({ page: String(Number(params().get("page") || 1) + 1) }));
  window.addEventListener("popstate", load);

  startLive(me, {
    relevant: () => true,
    describe: (n) => `${n} ${n === 1 ? "task has" : "tasks have"} changed since you loaded this list.`,
    onRefresh: load,
  });
  load();
}

// --------------------------------------------------------------- board ----

const COLUMN_NAMES = {
  awaiting_approval: "Awaiting approval", open: "Open", in_progress: "In progress",
  blocked: "Blocked", done: "Done", cancelled: "Cancelled",
};
const CHIPS = {
  mine: { group: "assignee", params: { assignee: "me" } },
  unassigned: { group: "assignee", params: { assignee: "unassigned" } },
  urgent: { group: "priority", params: { priority: "urgent,high" } },
  overdue: { group: "view", params: { view: "overdue" } },
  requested: { group: "view", params: { view: "requested_by_me" } },
};

async function initBoard() {
  const me = await topbar("board");
  flashPending();
  const teamSelect = $("#board-team");
  for (const t of me.teams) teamSelect.append(el("option", { value: t.id }, t.name));
  const url = () => new URLSearchParams(location.search);
  const activeChips = () => new Set((url().get("chips") || "").split(",").filter((c) => c in CHIPS));

  function filterParams() {
    const p = new URLSearchParams();
    for (const chip of activeChips()) for (const [k, v] of Object.entries(CHIPS[chip].params)) p.set(k, v);
    const u = url();
    if (u.get("team_id")) p.set("team_id", u.get("team_id"));
    if (u.get("q")) p.set("q", u.get("q"));
    return p;
  }

  function setUrl(changes) {
    const u = url();
    for (const [k, v] of Object.entries(changes)) (v ? u.set(k, v) : u.delete(k));
    history.replaceState(null, "", `${location.pathname}${u.toString() ? "?" + u : ""}`);
    load();
  }

  for (const chip of document.querySelectorAll("[data-chip]")) {
    chip.addEventListener("click", () => {
      const set = activeChips();
      const name = chip.dataset.chip;
      if (set.has(name)) set.delete(name);
      else {
        for (const other of [...set]) if (CHIPS[other].group === CHIPS[name].group) set.delete(other);
        set.add(name);
      }
      setUrl({ chips: [...set].join(",") });
    });
  }
  teamSelect.addEventListener("change", () => setUrl({ team_id: teamSelect.value }));
  let typing;
  $("#board-q").addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => setUrl({ q: $("#board-q").value.trim() }), 300);
  });

  const columns = {};   // status -> { node, list, countNode, count, page, more, loaded }
  // Which columns are folded. A per-browser convenience; the board works without it.
  let folded = new Set(["done", "cancelled"]);
  try {
    const saved = JSON.parse(localStorage.getItem("board.folded") || "null");
    if (Array.isArray(saved)) folded = new Set(saved.filter((s) => STATUSES.includes(s)));
  } catch { /* storage off */ }
  const saveFolded = () => { try { localStorage.setItem("board.folded", JSON.stringify([...folded])); } catch { /* storage off */ } };
  let dragging = null;  // { it, li } while a card is being dragged
  let loadSeq = 0;      // ignore columns from an older load

  function card(it) {
    const li = el("li", {
      class: `kcard p-${it.priority}`, draggable: nextStatuses(it).length ? "true" : null, "data-id": it.id,
    },
      el("a", { class: "title", href: `/item.html?id=${it.id}`, draggable: "false" }, it.title),
      el("div", { class: "meta" },
        prioBadge(it.priority), el("span", { class: "tag" }, label(it.type)),
        me.teams.length > 1 || it.my_role === "requester" ? el("span", { class: "tag" }, it.team.name) : null,
        it.restricted ? lockTag() : null,
        it.my_role === "requester" ? el("span", { class: "tag" }, "Your request") : null),
      el("div", { class: "foot" }, avatar(it.assignee),
        el("span", { class: "name" }, it.assignee ? it.assignee.name : "Unassigned"), dueChip(it, { whenFinished: false })),
      statusSelect(it, (updated) => moveCard(li, it, { ...it, ...updated })),
    );
    li.addEventListener("dragstart", (ev) => {
      dragging = { it, li };
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", it.id);
      li.classList.add("dragging");
      const ok = new Set(nextStatuses(it).map(([, to]) => to));
      for (const [status, col] of Object.entries(columns)) {
        col.node.classList.toggle("drop-ok", ok.has(status));
        col.node.classList.toggle("drop-no", !ok.has(status) && status !== it.status);
      }
    });
    li.addEventListener("dragend", () => {
      li.classList.remove("dragging");
      for (const col of Object.values(columns)) col.node.classList.remove("drop-ok", "drop-no");
      dragging = null;
    });
    return li;
  }

  function setCount(status, delta) {
    const col = columns[status];
    col.count += delta;
    col.countNode.textContent = col.count;
    col.list.querySelector(".empty")?.remove();
    if (col.list.children.length === 0) col.list.append(el("li", { class: "empty" }, "No tasks here"));
  }

  // Put the card in its (new) column once the server has accepted the change.
  function moveCard(li, before, after) {
    const fresh = card(after);
    if (after.status !== before.status) {
      li.remove();
      // A folded column that hasn't loaded yet will fetch it when opened.
      if (columns[after.status].loaded) columns[after.status].list.prepend(fresh);
      setCount(before.status, -1);
      setCount(after.status, +1);
    } else {
      li.replaceWith(fresh);
    }
  }

  async function drop(status) {
    if (!dragging) return;
    const { it, li } = dragging;
    const move = nextStatuses(it).find(([, to]) => to === status);
    if (!move) {
      if (status !== it.status) flash(`"${it.title}" can't go from ${label(it.status)} to ${label(status)} directly.`);
      return;
    }
    // Show the move straight away; put it back if the server says no.
    const home = li.parentElement;
    const next = li.nextSibling;
    li.classList.add("pending");
    columns[status].list.prepend(li);
    const updated = await runTransition(it, move[0]);
    li.classList.remove("pending");
    home.insertBefore(li, next); // back to where it was; moveCard does the real move + counts
    if (updated) moveCard(li, it, { ...it, ...updated });
  }

  async function loadColumn(status, page, seq) {
    const col = columns[status];
    const p = filterParams();
    p.set("status", status);
    p.set("page", page);
    p.set("page_size", 25);
    p.set("sort", TERMINAL.has(status) ? "updated" : "priority");
    const data = await api("GET", `/api/items?${p}`);
    if (seq !== loadSeq) return;
    if (page === 1) col.list.replaceChildren();
    col.list.append(...data.items.map(card));
    if (col.list.children.length === 0) col.list.append(el("li", { class: "empty" }, "No tasks here"));
    col.page = page;
    col.loaded = true;
    col.more.hidden = !data.has_more;
  }

  function setFolded(status, fold) {
    const col = columns[status];
    fold ? folded.add(status) : folded.delete(status);
    saveFolded();
    col.node.classList.toggle("folded", fold);
    col.node.setAttribute("aria-expanded", String(!fold));
    col.node.tabIndex = fold ? 0 : -1;
    col.node.title = fold ? `Open ${COLUMN_NAMES[status]}` : "";
    col.foldButton.hidden = fold;
    if (!fold && !col.loaded) loadColumn(status, 1, loadSeq).catch((err) => showError($("#board-error"), err));
  }

  async function load() {
    const seq = ++loadSeq;
    const chips = activeChips();
    for (const chip of document.querySelectorAll("[data-chip]")) chip.setAttribute("aria-pressed", String(chips.has(chip.dataset.chip)));
    teamSelect.value = url().get("team_id") || "";
    if (document.activeElement !== $("#board-q")) $("#board-q").value = url().get("q") || "";
    try {
      const counts = await api("GET", `/api/items/counts?${filterParams()}`);
      if (seq !== loadSeq) return;
      $("#board").replaceChildren(...STATUSES.map((status) => {
        const list = el("ol", { class: "cards" });
        const countNode = el("span", { class: "count" }, counts[status]);
        const more = el("button", { type: "button", class: "more", hidden: true,
          onclick: () => loadColumn(status, columns[status].page + 1, loadSeq) }, "Show more");
        const foldButton = el("button", { type: "button", class: "fold", title: `Fold ${COLUMN_NAMES[status]}`,
          "aria-label": `Fold ${COLUMN_NAMES[status]}`, onclick: (ev) => { ev.stopPropagation(); setFolded(status, true); } }, "‹");
        const node = el("section", { class: "column", style: `--c: var(--s-${status})`, "aria-label": COLUMN_NAMES[status] },
          el("header", {}, el("h2", {}, COLUMN_NAMES[status]), countNode, foldButton), list, more);
        // A folded column opens when clicked (or with Enter/Space).
        node.addEventListener("click", () => { if (folded.has(status)) setFolded(status, false); });
        node.addEventListener("keydown", (ev) => {
          if (folded.has(status) && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); setFolded(status, false); }
        });
        node.addEventListener("dragover", (ev) => {
          if (dragging && nextStatuses(dragging.it).some(([, to]) => to === status)) {
            ev.preventDefault();
            ev.dataTransfer.dropEffect = "move";
          }
        });
        node.addEventListener("drop", (ev) => { ev.preventDefault(); drop(status); });
        columns[status] = { node, list, countNode, count: counts[status], page: 1, more, foldButton, loaded: false };
        if (folded.has(status)) {
          node.classList.add("folded");
          node.tabIndex = 0;
          node.setAttribute("aria-expanded", "false");
          node.title = `Open ${COLUMN_NAMES[status]}`;
          foldButton.hidden = true;
        }
        return node;
      }));
      showError($("#board-error"), null);
      await Promise.all(STATUSES.filter((s) => !folded.has(s)).map((s) => loadColumn(s, 1, seq)));
    } catch (err) {
      showError($("#board-error"), err);
    }
  }

  startLive(me, {
    relevant: () => true,
    describe: (n) => `${n} ${n === 1 ? "task has" : "tasks have"} changed since you loaded the board.`,
    onRefresh: load,
  });
  load();
}

// ----------------------------------------------------------- new task ----

// Members of a team, or null if the caller can't list them (not in that team).
async function teamMembers(teamId) {
  try { return await api("GET", `/api/teams/${teamId}/members`); } catch { return null; }
}

// Fill an assignee <select>: every member of `teamId` (everyone = true), or just
// "me" (someone who may only take an unassigned task). Returns the user's role
// in that team (null if they're not in it).
async function fillAssignees(select, teamId, me, current = null, everyone = true) {
  const role = me.teams.find((t) => t.id === teamId)?.role || null;
  select.replaceChildren(el("option", { value: "" }, "Nobody yet"));
  if (role && everyone) {
    for (const m of (await teamMembers(teamId)) || []) select.append(el("option", { value: m.id }, m.name));
  } else if (role) {
    select.append(el("option", { value: me.user.id }, `Me (${me.user.name})`));
  }
  if (current && ![...select.options].some((o) => o.value === current.id)) {
    select.append(el("option", { value: current.id }, current.name));
  }
  select.value = current ? current.id : "";
  return role;
}

function teamOptions(select, me, allTeams, selected) {
  const mine = new Set(me.teams.map((t) => t.id));
  const others = allTeams.filter((t) => !mine.has(t.id));
  select.replaceChildren(
    el("optgroup", { label: "Your teams" }, me.teams.map((t) => el("option", { value: t.id }, t.name))),
    others.length ? el("optgroup", { label: "Other teams (sends them a request)" },
      others.map((t) => el("option", { value: t.id }, t.name))) : null,
  );
  if (selected) select.value = selected;
}

async function initNew() {
  const me = await topbar(null);
  const form = $("#new-form");
  const f = form.elements;
  const allTeams = await api("GET", "/api/teams");
  teamOptions(f.team_id, me, allTeams, me.teams[0]?.id);
  fillSelect(f.type, TYPES);
  fillSelect(f.priority, PRIORITIES);
  f.priority.value = "medium";
  const create = intent();

  async function syncTeam() {
    const role = await fillAssignees(f.assignee_id, f.team_id.value, me);
    $("#assignee-field").hidden = role === null;
    $("#assignee-hint").textContent = role ? "You'll be able to reassign it later; the person you assign can't pass it on." : "";
    // Leads can always edit, so restricting is only offered to other creators.
    $("#restrict-field").hidden = role === "lead";
    if (role === "lead") f.restricted.checked = false;
    $("#team-hint").textContent = role === null
      ? "You're not in this team, so this becomes a request: they do the work, and you can follow, comment on and edit it under Requested by me."
      : "";
  }
  f.team_id.addEventListener("change", syncTeam);
  await syncTeam();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = {
      team_id: f.team_id.value, title: f.title.value, description: f.description.value,
      type: f.type.value, priority: f.priority.value,
      requires_approval: f.requires_approval.checked, restricted: f.restricted.checked,
      due_at: f.due_at.value ? new Date(f.due_at.value).toISOString() : null,
      assignee_id: $("#assignee-field").hidden ? null : f.assignee_id.value || null,
    };
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        const item = await api("POST", "/api/items", body, { idempotencyKey: create.key() });
        create.settle(null);
        location.href = `/item.html?id=${item.id}`;
      } catch (err) {
        create.settle(err);
        showError($("#new-error"), err);
      }
    });
  });
  // A changed form is a new intent, not a retry of the old one.
  form.addEventListener("input", () => create.settle(null));
}

// ---------------------------------------------------------- item page ----

function describe(entry) {
  const p = entry.payload || {};
  switch (entry.kind) {
    case "created": return ["created this task"];
    case "comment": return ["commented", el("div", { class: "comment" }, p.body)];
    case "assigned":
      if (p.to && !p.from && p.to === entry.actor.id) return ["took this task"];
      if (p.to && p.from) return [`reassigned it from ${p.from_name} to ${p.to_name}`];
      if (p.to) return [`assigned it to ${p.to_name}`];
      return [`unassigned ${p.from_name || "the previous owner"}`];
    case "status_changed":
      return [`${PAST[p.action] || p.action} (${label(p.from)} → ${label(p.to)})`,
        p.reason ? el("div", { class: "reason" }, `Reason: ${p.reason}`) : null];
    case "updated": {
      const parts = Object.entries(p.changes || {}).map(([field, c]) => {
        if (field === "description") return "edited the description";
        if (field === "team") return `moved it from ${c.from} to ${c.to}`;
        if (field === "restricted") return c.to ? "restricted editing to leads and the creator" : "opened editing to the team";
        if (field === "due_at") return `changed the due date to ${c.to ? new Date(c.to).toLocaleString() : "none"}`;
        return `changed ${label(field).toLowerCase()} from “${c.from ?? "none"}” to “${c.to ?? "none"}”`;
      });
      return [parts.join(", ") || "updated the task"];
    }
    default: return [entry.kind];
  }
}

// The item's editable fields in the same flat shape the edit form produces.
const flat = (it) => ({
  title: it.title, description: it.description, priority: it.priority, due_at: it.due_at,
  restricted: it.restricted, team_id: it.team.id, assignee_id: it.assignee ? it.assignee.id : null,
});

async function initItem() {
  const me = await topbar(null);
  const id = new URLSearchParams(location.search).get("id");
  const allTeams = await api("GET", "/api/teams");
  let item = null;       // latest server state
  let editing = null;    // { baseVersion, snapshot } while the edit form is open
  let members = null;    // team members for the assign picker
  let olderCursor = null;

  async function refresh() {
    try {
      const fresh = await api("GET", `/api/items/${encodeURIComponent(id)}`);
      // An older copy (a request that raced a save) must never replace newer state.
      if (latest(item, fresh) !== fresh) return;
      if (item && item.team.id !== fresh.team.id) members = null;
      item = fresh;
      $("#item-root").hidden = false;
      showError($("#load-error"), null);
      render();
      if (editing && item.version > editing.baseVersion) {
        $("#banner").hidden = false;
        $("#banner").textContent = "This task changed while you were editing. Saving will show you both versions.";
      }
      loadTimeline();
    } catch (err) {
      if (err.status === 404) {
        $("#item-root").hidden = true;
        showError($("#load-error"), new Error("This task doesn't exist, or you don't have access to it."));
      } else if (!item) {
        showError($("#load-error"), err);
      }
    }
  }

  function render() {
    document.title = `${item.title} · Newtonite Ops`;
    $("#header").hidden = !!editing;
    $("#description").hidden = !!editing;
    $("#header").replaceChildren(
      el("h1", {}, item.title),
      el("div", { class: "badges" }, statusBadge(item.status), prioBadge(item.priority),
        el("span", { class: "tag" }, label(item.type)), el("span", { class: "tag" }, item.team.name),
        item.restricted ? lockTag("Locked: only leads and the creator edit details") : null,
        item.my_role === "requester" ? el("span", { class: "tag" }, "Your request to this team") : null),
    );
    $("#description").replaceChildren(item.description
      ? document.createTextNode(item.description)
      : el("span", { class: "muted" }, "No description."));
    renderMeta();
    $("#comment-form").hidden = !item.allowed_actions.includes("comment");
  }

  function renderMeta() {
    const can = (a) => item.allowed_actions.includes(a);
    const assignPicker = can("assign") ? el("select", { "aria-label": "Assign to", onchange: (ev) => assign(ev.target) },
      el("option", { value: "" }, "Change assignee")) : null;
    if (assignPicker) loadMembers().then((list) => {
      for (const m of list || []) assignPicker.append(el("option", { value: m.id }, m.name));
      if (item.assignee) assignPicker.append(el("option", { value: "none" }, "Unassign"));
    });
    $("#meta").replaceChildren(
      el("dl", {},
        el("dt", {}, "Status"), el("dd", {}, statusSelect(item, (updated) => { item = updated; render(); loadTimeline(); })),
        el("dt", {}, "Assignee"), el("dd", {}, el("span", { class: "who" }, avatar(item.assignee), item.assignee ? item.assignee.name : "Unassigned")),
        item.assignee && item.assigned_by ? el("dt", {}, "Assigned by") : null,
        item.assignee && item.assigned_by ? el("dd", {}, item.assigned_by.name) : null,
        el("dt", {}, "Created by"), el("dd", {}, item.created_by.name),
        el("dt", {}, "Created"), el("dd", {}, fmtDate(item.created_at)),
        el("dt", {}, "Due"), el("dd", {}, dueChip(item) || "None"),
        el("dt", {}, "Approval"), el("dd", {}, item.requires_approval ? "Required" : "Not required"),
        el("dt", {}, "Editing"), el("dd", {}, item.restricted ? "Leads and creator only" : "Anyone in the team"),
        el("dt", {}, "Last activity"), el("dd", {}, ago(item.last_activity_at)),
      ),
      el("div", { class: "actions" },
        can("claim") ? el("button", { type: "button", class: "primary", onclick: (ev) => claim(ev.target) }, "Take this task") : null,
        can("edit") && !editing ? el("button", { type: "button", onclick: openEditor }, "Edit") : null),
      ...(assignPicker ? [el("div", { class: "actions" }, assignPicker)] : []),
    );
  }

  async function loadMembers() {
    members ??= await teamMembers(item.team.id);
    return members;
  }

  async function loadTimeline(older = false) {
    const q = older && olderCursor ? `?before_id=${olderCursor}` : "";
    const page = await api("GET", `/api/items/${encodeURIComponent(id)}/activity${q}`);
    const rows = page.entries.map((e) => {
      const d = describe(e);
      const color = e.kind === "status_changed" ? `--c: var(--s-${e.payload.to})` : null;
      return el("li", { class: `k-${e.kind}`, style: color },
        el("strong", {}, e.actor.name), " ", d[0],
        el("span", { class: "when", title: fmtDate(e.created_at) }, ago(e.created_at)), ...d.slice(1));
    });
    older ? $("#entries").append(...rows) : $("#entries").replaceChildren(...rows);
    olderCursor = page.next_before_id;
    $("#older").hidden = !olderCursor;
  }

  // Optimistic: show the claim at once, then let the server's answer win.
  const claimIntent = intent();
  async function claim(button) {
    await busy(button, async () => {
      const before = item;
      item = { ...item, assignee: { id: me.user.id, name: me.user.name },
        allowed_actions: item.allowed_actions.filter((a) => a !== "claim") };
      render();
      try {
        item = await api("POST", `/api/items/${id}/claim`, null, { idempotencyKey: claimIntent.key() });
        claimIntent.settle(null);
        flash("You now own this task.", true);
      } catch (err) {
        claimIntent.settle(err);
        item = before;
        if (err.code === "ALREADY_CLAIMED") {
          item = { ...before, assignee: err.details.assignee };
          flash(`Already taken by ${err.details.assignee ? err.details.assignee.name : "someone else"}.`);
        } else {
          flash(err.message);
        }
        render();
      }
      await refresh();
    });
  }

  async function assign(select) {
    const value = select.value;
    if (!value) return;
    try {
      item = await api("POST", `/api/items/${id}/assign`,
        { assignee_id: value === "none" ? null : value, expected_version: item.version }, { idempotencyKey: newKey() });
      flash("Assignee updated.", true);
    } catch (err) {
      flash(err.code === "STALE_VERSION" ? "This task changed since you loaded it. Refresh and try again." : err.message);
    }
    await refresh();
  }

  // ---- editing with stale-write protection ----
  const toLocalInput = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };

  async function openEditor() {
    editing = { baseVersion: item.version };
    const can = (a) => item.allowed_actions.includes(a);
    const teamSelect = el("select", { name: "team_id" });
    teamOptions(teamSelect, me, allTeams, item.team.id);
    const assigneeSelect = el("select", { name: "assignee_id" });
    const assigneeField = el("label", {}, "Assign to", assigneeSelect,
      el("span", { class: "hint" }, "Leads and whoever assigned the task can reassign it. The person it's assigned to can't pass it on."));
    const form = el("form", { class: "card stack", id: "edit-form" },
      el("label", {}, "Title", el("input", { name: "title", value: item.title, maxlength: 200, required: true })),
      el("label", {}, "Description", el("textarea", { name: "description", rows: 8, maxlength: 20000 })),
      el("div", { class: "row" },
        el("label", {}, "Priority", el("select", { name: "priority" }, PRIORITIES.map((p) => el("option", { value: p }, label(p))))),
        el("label", {}, "Due", el("input", { name: "due_at", type: "datetime-local", value: toLocalInput(item.due_at) }))),
      el("label", {}, "Team", teamSelect,
        el("span", { class: "hint" }, "Moving it to another team hands the work over. If the assignee isn't in that team, it becomes unassigned.")),
      assigneeField,
      can("restrict") ? el("label", { class: "check" },
        el("input", { name: "restricted", type: "checkbox", checked: item.restricted }),
        "Restrict editing: other members can't change the details (team leads and you still can)") : null,
      el("div", { id: "conflict" }),
      el("div", { class: "row" },
        el("button", { type: "submit", class: "primary" }, "Save changes"),
        el("button", { type: "button", onclick: closeEditor }, "Cancel")),
    );
    form.elements.description.value = item.description;
    form.elements.priority.value = item.priority;

    // Who can be assigned depends on the team the form currently points at.
    async function syncAssignees() {
      const sameTeam = teamSelect.value === item.team.id;
      const role = await fillAssignees(assigneeSelect, teamSelect.value, me, sameTeam ? item.assignee : null, can("assign"));
      if (sameTeam) {
        // Leads and the assigner pick anyone; others may only take an unassigned task.
        assigneeField.hidden = !(can("assign") || can("claim"));
      } else {
        // Moving: pick someone in the new team only if you may assign and are in that team.
        assigneeField.hidden = !(can("assign") && role !== null);
      }
    }
    teamSelect.addEventListener("change", syncAssignees);
    await syncAssignees();

    form.addEventListener("submit", (ev) => { ev.preventDefault(); save(editing.baseVersion); });
    $("#editor").replaceChildren(form);
    $("#editor").hidden = false;
    editing.snapshot = flat(item); // what the task was when editing began
    render();
  }

  function closeEditor() {
    editing = null;
    $("#editor").hidden = true;
    $("#editor").replaceChildren();
    $("#banner").hidden = true;
    render();
  }

  function draft() {
    const f = $("#edit-form").elements;
    const out = {
      title: f.title.value.trim(),
      description: f.description.value,
      priority: f.priority.value,
      due_at: f.due_at.value ? new Date(f.due_at.value).toISOString() : null,
      team_id: f.team_id.value,
    };
    if (f.restricted) out.restricted = f.restricted.checked;
    if (!f.assignee_id.closest("label").hidden) out.assignee_id = f.assignee_id.value || null;
    return out;
  }

  // The datetime input drops seconds; don't treat that as an edit.
  function myChanges() {
    const changes = changedFields(editing.snapshot, draft());
    if ("due_at" in changes && editing.snapshot.due_at && changes.due_at &&
        toLocalInput(editing.snapshot.due_at) === toLocalInput(changes.due_at)) delete changes.due_at;
    return changes;
  }

  async function save(expectedVersion) {
    const button = $("#edit-form button[type=submit]");
    const changes = myChanges();
    if (Object.keys(changes).length === 0) { closeEditor(); flash("Nothing to save.", true); return; }
    await busy(button, async () => {
      try {
        const saved = await api("PATCH", `/api/items/${id}`, { expected_version: expectedVersion, ...changes });
        if (saved.allowed_actions.length === 0) {
          flashNext(`"${saved.title}" moved to ${saved.team.name}. You're not in that team, so it's no longer on your list.`);
          location.href = "/";
          return;
        }
        if ("team_id" in changes) members = null;
        item = saved;
        closeEditor();
        flash("Changes saved.", true);
        loadTimeline();
      } catch (err) {
        if (err.code === "STALE_VERSION") showConflict(err.details);
        else flash(err.message);
      }
    });
  }

  const shown = (field, value, current) => {
    if (value === null || value === undefined || value === "") return "None";
    if (field === "team_id") return allTeams.find((t) => t.id === value)?.name || value;
    if (field === "assignee_id") {
      return (members || []).find((m) => m.id === value)?.name
        || (current.assignee && current.assignee.id === value ? current.assignee.name : null)
        || (value === me.user.id ? me.user.name : "Someone");
    }
    if (field === "restricted") return value ? "Leads and creator only" : "Anyone in the team";
    if (field === "due_at") return new Date(value).toLocaleString();
    return String(value);
  };
  const FIELD_NAMES = { team_id: "Team", assignee_id: "Assignee", restricted: "Editing", due_at: "Due" };

  // Keep the user's draft; show only the fields they changed next to the server's values now.
  function showConflict({ current, changed_by }) {
    item = current;
    renderMeta();
    const rows = conflictRows(myChanges(), flat(current)).map((r) =>
      el("tr", {}, el("th", {}, FIELD_NAMES[r.field] || label(r.field)),
        el("td", {}, shown(r.field, r.mine, current)), el("td", {}, shown(r.field, r.current, current))));
    $("#banner").hidden = true;
    $("#conflict").replaceChildren(el("div", { class: "conflict" },
      el("strong", {}, changed_by && changed_by.id === me.user.id
        ? "You changed this task somewhere else (another tab or window) while this editor was open."
        : `${changed_by ? changed_by.name : "Someone"} changed this task while you were editing.`),
      rows.length
        ? el("table", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Yours"), el("th", {}, "Current")), rows)
        : el("p", {}, "Your edits don't clash with theirs. Save again to apply them on top."),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", onclick: () => { editing.baseVersion = current.version; save(current.version); } }, "Keep my changes"),
        el("button", { type: "button", onclick: () => { closeEditor(); refresh(); } }, "Discard my changes")),
    ));
  }

  // ---- comments ----
  const commentIntent = intent();
  $("#comment-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    await busy(form.querySelector("button"), async () => {
      try {
        await api("POST", `/api/items/${id}/comments`, { body: form.elements.body.value }, { idempotencyKey: commentIntent.key() });
        commentIntent.settle(null);
        form.reset();
        await refresh();
      } catch (err) {
        commentIntent.settle(err);
        flash(err.message);
      }
    });
  });
  $("#comment-form").addEventListener("input", () => commentIntent.settle(null));
  $("#older").addEventListener("click", () => loadTimeline(true));

  // No timed polling: the server tells us when this task changes, and we
  // offer a refresh. Saving with an old version is still refused (409).
  startLive(me, {
    relevant: (ev) => ev.item_id === id,
    describe: () => "Someone else just updated this task.",
    onRefresh: refresh,
  });
  await refresh();
}

// --------------------------------------------------------------- boot ----

const pages = {
  login: initLogin, signup: initSignup, overview: initOverview, board: initBoard, new: initNew, item: initItem,
};
const init = pages[document.body.dataset.page];
if (init) init().catch((err) => console.error(err));
