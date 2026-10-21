// Newtonite Ops frontend. Plain JS modules, no build step.
// Server state is fetched per page; the URL holds filter state; every
// mutation carries an Idempotency-Key so a retry can never act twice; live
// updates arrive by server push and only ever *offer* a refresh.

import { changedFields, conflictRows } from "./edits.js";
import { safeNext } from "./redirect.js";
import { initAdmin } from "./admin.js";
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
    if (res.status === 403 && e.code === "PASSWORD_CHANGE_REQUIRED" && !location.pathname.endsWith("/change-password.html")) {
      location.href = "/change-password.html";
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
const PRIORITIES = ["urgent", "high", "medium", "low"];
// Colours a team can give its statuses (the --col-* variables in style.css).
const COLORS = ["blue", "violet", "green", "teal", "amber", "orange", "red", "pink", "slate"];
const colorVar = (c) => `var(--col-${COLORS.includes(c) ? c : "slate"})`;
// Wording for history entries written before statuses became per-team.
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
const statusBadge = (s) => el("span", { class: "badge st", style: `--c: ${colorVar(s.color)}` },
  el("span", { class: "status-dot" }), s.name);
const prioBadge = (p) => el("span", { class: `badge pr pr-${p}` }, label(p));

// A lead has to approve some tasks before their status can change.
function approvalTag(item) {
  if (item.approval === "pending") {
    return el("span", { class: "tag approval-pending", title: "A team lead has to approve this task before work can start" },
      "Awaiting approval");
  }
  if (item.approval === "rejected") {
    return el("span", { class: "tag approval-rejected", title: "A team lead rejected this task" }, "Rejected");
  }
  return null;
}

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

// `whenFinished: false` hides the chip on finished tasks, where a due date no longer matters.
function dueChip(item, { whenFinished = true } = {}) {
  if (!item.due_at) return null;
  if (!whenFinished && item.closed) return null;
  const ms = new Date(item.due_at).getTime() - Date.now();
  const open = !item.closed;
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

// In-app replacement for confirm() and prompt(): a modal that resolves with the answer.
// `input` ({ label, placeholder }) adds a text box. Resolves null when cancelled (Esc, Cancel).
// `choices` ({ label, options: [{ value, label }] }) adds a list to pick from; the answer is the chosen value.
function askModal({ title, message = null, confirmLabel = "Confirm", danger = false, input = null, choices = null }) {
  return new Promise((resolve) => {
    let answer = null;
    const field = input
      ? (input.single ? el("input", { value: input.value || "", maxlength: 40, required: true })
        : el("textarea", { rows: 3, maxlength: 2000, placeholder: input.placeholder || "" }))
      : choices ? el("select", {}, choices.options.map((o) => el("option", { value: o.value }, o.label))) : null;
    const form = el("form", { class: "stack" },
      el("h2", {}, title),
      message ? el("p", {}, message) : null,
      field ? el("label", {}, (input || choices).label, field) : null,
      el("div", { class: "row ask-actions" },
        el("button", { type: "button", onclick: () => dialog.close() }, "Cancel"),
        el("button", { type: "submit", class: danger ? "danger" : "primary" }, confirmLabel)));
    const dialog = el("dialog", { class: "ask-dialog", "aria-label": title }, form);
    form.addEventListener("submit", (ev) => { ev.preventDefault(); answer = field ? field.value.trim() : true; dialog.close(); });
    // (a chosen value can be "": that still means "confirmed")
    dialog.addEventListener("close", () => { dialog.remove(); resolve(answer); });
    document.body.append(dialog);
    dialog.showModal();
  });
}
const confirmModal = async (options) => (await askModal(options)) !== null;
const promptModal = (options) => askModal(options);

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
const setRefresh = (fn) => { refreshHandler = fn; };

// Each team's statuses, from /api/me (re-read whenever a page refreshes).
let statusesByTeam = {};
function setStatuses(me) {
  statusesByTeam = Object.fromEntries(me.teams.map((t) => [t.id, t.statuses || []]));
}
const teamStatuses = (teamId) => statusesByTeam[teamId] || [];

async function topbar(active) {
  const me = await api("GET", "/api/me");
  setStatuses(me);
  const tab = (href, text, key) => el("a", { href, "aria-current": active === key ? "page" : null }, text);
  $("#topbar").replaceChildren(
    el("a", { class: "brand", href: "/" }, "Newtonite Ops"),
    el("nav", { class: "tabs", "aria-label": "Views" }, tab("/", "List", "list"), tab("/board.html", "Board", "board"),
      me.user.is_admin ? tab("/admin.html", "Admin", "admin") : null),
    el("span", { class: "spacer" }),
    el("button", { type: "button", id: "refresh-btn", class: "with-icon", title: "Load the latest data",
      onclick: () => refreshHandler() }, icon(REFRESH_ICON), "Refresh"),
    el("a", { href: "/new.html" }, el("button", { type: "button", class: "primary", tabindex: "-1" }, "New task")),
    el("a", { class: "me", href: "/profile.html", title: "Your profile", "aria-current": active === "profile" ? "page" : null },
      avatar(me.user), el("span", { class: "me-name" }, me.user.name)),
    el("button", { type: "button", onclick: async () => { await api("POST", "/api/logout"); location.href = "/login.html"; } }, "Log out"),
  );
  return me;
}

// ------------------------------------------------------- live updates ----
// The server pushes "something changed" notices (SSE). We never redraw on our
// own: we count the tasks others changed and offer a Refresh.

function startLive(me, { relevant, describe, onRefresh }) {
  const banner = $("#live-banner");
  const pending = new Set(); // ids of tasks others changed (one task moved 4 times = 1)
  let statusesChanged = false;
  let missed = false;
  let hadError = false;

  const show = () => {
    if (!pending.size && !missed && !statusesChanged) { banner.hidden = true; return; }
    const text = missed
      ? "The live connection dropped for a moment, so you may have missed changes."
      : pending.size ? describe(pending.size) + (statusesChanged ? " The team's statuses changed too." : "")
        : "A team lead changed this team's statuses.";
    banner.replaceChildren(el("span", {}, text),
      el("button", { type: "button", class: "primary", onclick: () => refreshHandler() }, "Refresh"));
    banner.hidden = false;
  };
  refreshHandler = async () => {
    pending.clear(); missed = false; statusesChanged = false; show();
    await onRefresh();
  };

  const source = new EventSource("/api/events");
  source.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }
    if (ev.type === "hello") {
      if (hadError) { missed = true; show(); } // reconnected: we can't know what we missed
      hadError = false;
    } else if (ev.type === "resync") {
      missed = true; show();
    } else if (ev.type === "change" && ev.actor_id !== me.user.id && relevant(ev)) {
      if (ev.kind === "statuses") statusesChanged = true;
      else pending.add(ev.item_id);
      show();
    }
  };
  source.onerror = () => { hadError = true; };
}

// ------------------------------------------------------ status changes ----

const canMove = (item) => item.allowed_actions.includes("move");

const moveIntents = new Map();
// Move a task to `status` (any status of its team). Returns the updated task, or null.
async function moveItem(item, status) {
  if (status.kind === "unassigned" && item.status.kind !== "unassigned") {
    const ok = await confirmModal({
      title: `Move to ${status.name}?`,
      message: `"${item.title}" will move to ${status.name}.` + (item.assignee ? ` Its assignee, ${item.assignee.name}, will be removed from it.` : ""),
      confirmLabel: item.assignee ? "Remove assignee and move" : "Move",
      danger: !!item.assignee,
    });
    if (!ok) return null;
  }
  // Leaving Unassigned for a real status: ask who should work on it (whoever may assign it).
  let assigneeId = null;
  if (item.status.kind === "unassigned" && status.kind !== "unassigned" && !item.assignee && item.allowed_actions.includes("assign")) {
    const people = (await teamMembers(item.team.id)) || [];
    if (people.length) {
      const picked = await askModal({
        title: "Assign this task?",
        message: `"${item.title}" is leaving ${item.status.name} for ${status.name}. Who should work on it?`,
        choices: { label: "Assign to", options: [{ value: "", label: "Nobody yet" }, ...people.map((p) => ({ value: p.id, label: p.name }))] },
        confirmLabel: "Move task",
      });
      if (picked === null) return null;
      assigneeId = picked || null;
    }
  }
  const k = `${item.id}:${status.id}:${item.version}:${assigneeId || ""}`;
  const it = moveIntents.get(k) ?? intent();
  moveIntents.set(k, it);
  try {
    const updated = await api("POST", `/api/items/${item.id}/status`,
      { status_id: status.id, expected_version: item.version, ...(assigneeId ? { assignee_id: assigneeId } : {}) },
      { idempotencyKey: it.key() });
    it.settle(null);
    moveIntents.delete(k);
    flash(`"${updated.title}" moved to ${status.name}.`, true);
    return updated;
  } catch (err) {
    it.settle(err);
    flash(err.code === "STALE_VERSION"
      ? `"${item.title}" was changed by someone else. Refresh to see the latest, then try again.`
      : err.message);
    return null;
  }
}

// The status as a dropdown listing every status of the task's team, or a plain
// pill when this user can't change it.
function statusSelect(item, onUpdated) {
  const options = teamStatuses(item.team.id);
  if (!canMove(item) || options.length === 0) {
    const badge = statusBadge(item.status);
    badge.classList.add("status-pill");
    badge.title = item.approval === "pending" ? "A team lead has to approve this task first"
      : item.approval === "rejected" ? "A lead rejected this task"
        : "You can't change this task's status";
    return badge;
  }
  const select = el("select", { class: "status-select status-pill st", style: `--c: ${colorVar(item.status.color)}`,
    "aria-label": `Change status of ${item.title}` },
    options.map((s) => el("option", { value: s.id }, s.name)));
  select.value = item.status.id;
  select.addEventListener("change", async () => {
    const target = options.find((s) => s.id === select.value);
    select.disabled = true;
    const updated = target ? await moveItem(item, target) : null;
    select.disabled = false;
    if (updated) onUpdated(updated);
    else select.value = item.status.id; // refused or failed: show the real status again
  });
  return select;
}

// ------------------------------------------------------- sign-in flow ----

// Where to go once signed in: an admin who is in no team has nothing to see on the
// task pages, so they land on the admin dashboard.
async function landingPath(user) {
  if (user.must_change_password) return "/change-password.html";
  if (user.is_admin) {
    const me = await api("GET", "/api/me");
    if (me.teams.length === 0) return "/admin.html";
  }
  return "/";
}

async function initLogin() {
  const form = $("#login-form");
  try {
    // A brand-new installation has no accounts: send the visitor to the one-time setup page.
    if ((await api("GET", "/api/setup")).needed) { location.replace("/setup.html"); return; }
  } catch { /* the form still works; the server will say what is wrong */ }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    await busy(form.querySelector("button"), async () => {
      try {
        const { user } = await api("POST", "/api/login", data);
        const next = new URLSearchParams(location.search).get("next");
        location.href = user.must_change_password ? "/change-password.html"
          : next ? safeNext(next, location.origin) : await landingPath(user);
      } catch (err) {
        showError($("#login-error"), err);
      }
    });
  });
}

// First run only: create the first admin on an empty database.
async function initSetup() {
  const form = $("#setup-form");
  let needed = true;
  try { needed = (await api("GET", "/api/setup")).needed; } catch { /* show the form; submitting reports errors */ }
  if (!needed) { location.replace("/login.html"); return; }
  form.hidden = false;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = form.elements;
    if (f.password.value !== f.confirm.value) {
      showError($("#setup-error"), new Error("The two passwords don't match."));
      return;
    }
    await busy(form.querySelector("button"), async () => {
      try {
        await api("POST", "/api/setup", { name: f.name.value, email: f.email.value, password: f.password.value });
        location.href = "/admin.html";
      } catch (err) {
        showError($("#setup-error"), err.code === "VALIDATION_FAILED"
          ? new Error("Check the form: a valid email and a password of at least 8 characters.") : err);
      }
    });
  });
}

// Shown to anyone whose password is a temporary one set by an admin.
async function initChangePassword() {
  const me = await api("GET", "/api/me");
  if (!me.user.must_change_password) { location.replace("/"); return; }
  const form = $("#password-form");
  form.hidden = false;
  $("#logout-btn").addEventListener("click", async () => { await api("POST", "/api/logout"); location.href = "/login.html"; });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = form.elements;
    if (f.new_password.value !== f.confirm.value) {
      showError($("#password-error"), new Error("The new passwords don't match."));
      return;
    }
    await busy(form.querySelector("button[type=submit]"), async () => {
      try {
        await api("POST", "/api/profile/password", { current_password: f.current_password.value, new_password: f.new_password.value });
        location.href = await landingPath({ ...me.user, must_change_password: false });
      } catch (err) {
        showError($("#password-error"), err.code === "VALIDATION_FAILED"
          ? new Error("The new password needs at least 8 characters.") : err);
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

// Fill a task type <select> with one team's types. Picks `currentId` if the team has it, else the type
// named `preferName`, else the first. Returns how many types there are, or null if a newer call owns the select.
async function fillTypeOptions(select, teamId, { currentId = null, preferName = "Task" } = {}) {
  select.dataset.team = teamId;
  let list = [];
  try { list = await api("GET", `/api/teams/${encodeURIComponent(teamId)}/types`); } catch { /* none to offer */ }
  if (select.dataset.team !== teamId) return null;
  select.replaceChildren(...list.map((t) => el("option", { value: t.id }, t.name)));
  const pick = list.find((t) => t.id === currentId) || list.find((t) => t.name.toLowerCase() === preferName.toLowerCase()) || list[0];
  if (pick) select.value = pick.id;
  return list.length;
}

// Fill a project <select> with the projects of one team ("No project" first). Returns how many
// there are, or null if the team changed while the request was running (a newer call owns the select).
async function fillProjectOptions(select, teamId, current = null) {
  select.dataset.team = teamId;
  let list = [];
  try { list = await api("GET", `/api/projects?team_id=${encodeURIComponent(teamId)}`); } catch { /* none to offer */ }
  if (select.dataset.team !== teamId) return null;
  select.replaceChildren(el("option", { value: "" }, "No project"), ...list.map((p) => el("option", { value: p.id }, p.name)));
  select.value = current && list.some((p) => p.id === current) ? current : "";
  return list.length;
}

function projectTag(project) {
  return el("span", { class: "tag project", title: "Project" }, project.name);
}

// Every project of the teams I'm in, once each (for the project filter).
function myProjects(me) {
  const seen = new Map();
  for (const t of me.teams) for (const p of t.projects || []) seen.set(p.id, p);
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function titleCell(it) {
  return el("td", { class: "title" },
    el("a", { href: `/item.html?id=${it.id}` }, it.title),
    it.restricted ? [" ", lockTag()] : null,
    it.project ? [" ", projectTag(it.project)] : null,
    approvalTag(it) ? [" ", approvalTag(it)] : null,
    it.my_role === "requester" ? [" ", el("span", { class: "tag" }, "Your request")] : null);
}

async function initOverview() {
  const me = await topbar("list");
  flashPending();
  const form = $("#filters");
  for (const t of me.teams) form.elements.team_id.append(el("option", { value: t.id }, t.name));
  // Status filter: every status name across my teams (matched by name, in any team that has it).
  const seen = new Set();
  for (const t of me.teams) for (const s of t.statuses) {
    if (!seen.has(s.name.toLowerCase())) {
      seen.add(s.name.toLowerCase());
      form.elements.status.append(el("option", { value: s.name }, s.name));
    }
  }
  fillSelect(form.elements.priority, PRIORITIES);
  for (const p of myProjects(me)) form.elements.project.append(el("option", { value: p.id }, p.name));

  const FIELDS = ["q", "team_id", "project", "status", "priority", "assignee", "sort"];
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
    if (me.teams.length === 0) {
      $("#tiles").hidden = true;
      $("#summary").replaceChildren(...(me.user.is_admin
        ? ["You're not in any team yet. ", el("a", { href: "/admin.html" }, "Open the admin dashboard"), " to add yourself to one."]
        : ["You're not in a team yet. Ask an admin to add you to one."]));
      return;
    }
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
  for (const f of ["team_id", "project", "status", "priority", "assignee", "sort"]) {
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

const CHIPS = {
  mine: { group: "assignee", params: { assignee: "me" } },
  unassigned: { group: "assignee", params: { assignee: "unassigned" } },
  urgent: { group: "priority", params: { priority: "urgent,high" } },
  overdue: { group: "view", params: { view: "overdue" } },
};

async function initBoard() {
  const me = await topbar("board");
  flashPending();
  const board = $("#board");
  if (me.teams.length === 0) {
    $("#board-tools").hidden = true;
    board.replaceChildren(el("p", { class: "empty-state" },
      "You're not in a team yet, so there's no board to show. Join or create a team to see its tasks here."));
    return;
  }

  // The board shows one team at a time: every team has its own statuses.
  const teamSelect = $("#board-team");
  const projectSelect = $("#board-project");
  teamSelect.replaceChildren(...me.teams.map((t) => el("option", { value: t.id }, t.name)));
  const url = () => new URLSearchParams(location.search);
  const teamId = () => (me.teams.some((t) => t.id === url().get("team_id")) ? url().get("team_id") : me.teams[0].id);
  const team = () => me.teams.find((t) => t.id === teamId());
  // The project filter: one of this team's projects, or "none"; anything else means "all".
  const projectId = () => {
    const v = url().get("project");
    return v === "none" || team().projects?.some((p) => p.id === v) ? v : "";
  };
  function fillProjectFilter() {
    projectSelect.replaceChildren(el("option", { value: "" }, "All projects"), el("option", { value: "none" }, "No project"),
      ...(team().projects || []).map((p) => el("option", { value: p.id }, p.name)));
    projectSelect.value = projectId();
  }
  const activeChips = () => new Set((url().get("chips") || "").split(",").filter((c) => c in CHIPS));

  function filterParams() {
    const p = new URLSearchParams();
    for (const chip of activeChips()) for (const [k, v] of Object.entries(CHIPS[chip].params)) p.set(k, v);
    p.set("team_id", teamId());
    if (projectId()) p.set("project", projectId());
    if (url().get("q")) p.set("q", url().get("q"));
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
  teamSelect.addEventListener("change", () => setUrl({ team_id: teamSelect.value, project: "" }));
  projectSelect.addEventListener("change", () => setUrl({ project: projectSelect.value }));
  let typing;
  $("#board-q").addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => setUrl({ q: $("#board-q").value.trim() }), 300);
  });

  const columns = {};   // status id -> { status, node, list, countNode, count, page, more, foldButton, loaded }
  // Which columns are folded, by status id. A per-browser convenience; finished statuses start folded.
  let folded = {};
  try { folded = JSON.parse(localStorage.getItem("board.folded.v2") || "{}") || {}; } catch { /* storage off */ }
  const isFolded = (s) => (s.id in folded ? folded[s.id] : s.finished);
  const saveFolded = () => { try { localStorage.setItem("board.folded.v2", JSON.stringify(folded)); } catch { /* storage off */ } };
  let dragging = null;  // { it, li } while a card is being dragged
  let loadSeq = 0;      // ignore columns from an older load

  function card(it) {
    const li = el("li", {
      class: `kcard p-${it.priority}`, draggable: canMove(it) ? "true" : null, "data-id": it.id,
    },
      el("a", { class: "title", href: `/item.html?id=${it.id}`, draggable: "false" }, it.title),
      el("div", { class: "meta" },
        prioBadge(it.priority), el("span", { class: "tag" }, it.type.name),
        it.project ? projectTag(it.project) : null,
        approvalTag(it),
        it.restricted ? lockTag() : null),
      el("div", { class: "foot" }, avatar(it.assignee),
        el("span", { class: "name" }, it.assignee ? it.assignee.name : "Unassigned"), dueChip(it, { whenFinished: false })),
      statusSelect(it, (updated) => moveCard(li, it, { ...it, ...updated })),
    );
    li.addEventListener("dragstart", (ev) => {
      dragging = { it, li };
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", it.id);
      li.classList.add("dragging");
      for (const col of Object.values(columns)) col.node.classList.toggle("drop-ok", col.status.id !== it.status.id);
    });
    li.addEventListener("dragend", () => {
      li.classList.remove("dragging");
      for (const col of Object.values(columns)) col.node.classList.remove("drop-ok", "drop-no");
      dragging = null;
    });
    return li;
  }

  function setCount(statusId, delta) {
    const col = columns[statusId];
    if (!col) return;
    col.count += delta;
    col.countNode.textContent = col.count;
    col.list.querySelector(".empty")?.remove();
    if (col.list.children.length === 0) col.list.append(el("li", { class: "empty" }, "No tasks here"));
  }

  // Put the card in its (new) column once the server has accepted the change.
  function moveCard(li, before, after) {
    const fresh = card(after);
    if (after.status.id !== before.status.id) {
      li.remove();
      // A folded column that hasn't loaded yet will fetch it when opened.
      if (columns[after.status.id]?.loaded) columns[after.status.id].list.prepend(fresh);
      setCount(before.status.id, -1);
      setCount(after.status.id, +1);
    } else {
      li.replaceWith(fresh);
    }
  }

  async function drop(statusId) {
    if (!dragging) return;
    const { it, li } = dragging;
    const target = columns[statusId]?.status;
    if (!target || target.id === it.status.id || !canMove(it)) return;
    // Show the move straight away; put it back if the server says no.
    const home = li.parentElement;
    const next = li.nextSibling;
    li.classList.add("pending");
    columns[statusId].list.prepend(li);
    const updated = await moveItem(it, target);
    li.classList.remove("pending");
    home.insertBefore(li, next); // back to where it was; moveCard does the real move + counts
    if (updated) moveCard(li, it, { ...it, ...updated });
  }

  async function loadColumn(statusId, page, seq) {
    const col = columns[statusId];
    const p = filterParams();
    p.set("status_id", statusId);
    p.set("page", page);
    p.set("page_size", 25);
    p.set("sort", col.status.finished ? "updated" : "priority");
    const data = await api("GET", `/api/items?${p}`);
    if (seq !== loadSeq) return;
    if (page === 1) col.list.replaceChildren();
    col.list.append(...data.items.map(card));
    if (col.list.children.length === 0) col.list.append(el("li", { class: "empty" }, "No tasks here"));
    col.page = page;
    col.loaded = true;
    col.more.hidden = !data.has_more;
  }

  function setFolded(statusId, fold) {
    const col = columns[statusId];
    folded[statusId] = fold;
    saveFolded();
    col.node.classList.toggle("folded", fold);
    col.node.setAttribute("aria-expanded", String(!fold));
    col.node.tabIndex = fold ? 0 : -1;
    col.node.title = fold ? `Open ${col.status.name}` : "";
    col.foldButton.hidden = fold;
    if (!fold && !col.loaded) loadColumn(statusId, 1, loadSeq).catch((err) => showError($("#board-error"), err));
  }

  function buildColumn(s, count) {
    const list = el("ol", { class: "cards" });
    const countNode = el("span", { class: "count" }, count);
    const more = el("button", { type: "button", class: "more", hidden: true,
      onclick: () => loadColumn(s.id, columns[s.id].page + 1, loadSeq) }, "Show more");
    const foldButton = el("button", { type: "button", class: "fold", title: `Fold ${s.name}`,
      "aria-label": `Fold ${s.name}`, onclick: (ev) => { ev.stopPropagation(); setFolded(s.id, true); } }, "‹");
    const node = el("section", { class: "column", style: `--c: ${colorVar(s.color)}`, "aria-label": s.name },
      el("header", {}, el("h2", { title: s.name }, s.name), countNode, foldButton), list, more);
    // A folded column opens when clicked (or with Enter/Space).
    node.addEventListener("click", () => { if (isFolded(s) && columns[s.id]?.node === node) setFolded(s.id, false); });
    node.addEventListener("keydown", (ev) => {
      if (isFolded(s) && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); setFolded(s.id, false); }
    });
    node.addEventListener("dragover", (ev) => {
      if (dragging && canMove(dragging.it) && dragging.it.status.id !== s.id) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
      }
    });
    node.addEventListener("drop", (ev) => { ev.preventDefault(); drop(s.id); });
    columns[s.id] = { status: s, node, list, countNode, count, page: 1, more, foldButton, loaded: false };
    if (isFolded(s)) {
      node.classList.add("folded");
      node.tabIndex = 0;
      node.setAttribute("aria-expanded", "false");
      node.title = `Open ${s.name}`;
      foldButton.hidden = true;
    }
    return node;
  }

  async function load() {
    const seq = ++loadSeq;
    const chips = activeChips();
    for (const chip of document.querySelectorAll("[data-chip]")) chip.setAttribute("aria-pressed", String(chips.has(chip.dataset.chip)));
    if (document.activeElement !== $("#board-q")) $("#board-q").value = url().get("q") || "";
    try {
      // A lead may have changed the statuses, so read them again on every load.
      const fresh = await api("GET", "/api/me");
      me.teams = fresh.teams;
      setStatuses(me);
      teamSelect.value = teamId();
      fillProjectFilter();
      $("#manage-statuses").hidden = team().role !== "lead";
      const counts = await api("GET", `/api/items/counts?${filterParams()}`);
      if (seq !== loadSeq) return;
      for (const k of Object.keys(columns)) delete columns[k];
      const statuses = team().statuses;
      board.replaceChildren(...statuses.map((s) => buildColumn(s, counts[s.id] || 0)));
      showError($("#board-error"), null);
      await Promise.all(statuses.filter((s) => !isFolded(s)).map((s) => loadColumn(s.id, 1, seq)));
    } catch (err) {
      showError($("#board-error"), err);
    }
  }

  // ---- managing the team's statuses (leads only) ----
  const dialog = $("#status-dialog");

  // Run a change, then reload the board and redraw the dialog from fresh data.
  async function manage(fn) {
    try {
      await fn();
      showError($("#status-error"), null);
      await load();
      renderManager();
    } catch (err) {
      showError($("#status-error"), err);
    }
  }

  const statusUrl = (s) => `/api/teams/${team().id}/statuses${s ? "/" + s.id : ""}`;

  async function tryDelete(s, row) {
    try {
      await api("DELETE", statusUrl(s));
      await load();
      renderManager();
    } catch (err) {
      if (err.code !== "STATUS_IN_USE") { showError($("#status-error"), err); return; }
      // Tasks are still in it: ask where they should go.
      const pick = el("select", { "aria-label": "Move its tasks to" },
        team().statuses.filter((x) => x.id !== s.id).map((x) => el("option", { value: x.id }, x.name)));
      const n = err.details.task_count;
      row.replaceChildren(
        el("span", { class: "status-row-note" }, `${n} ${n === 1 ? "task is" : "tasks are"} in "${s.name}". Move them to:`),
        pick,
        el("button", { type: "button", class: "danger",
          onclick: () => manage(() => api("DELETE", `${statusUrl(s)}?move_to=${pick.value}`)) }, "Move and delete"),
        el("button", { type: "button", onclick: renderManager }, "Cancel"));
    }
  }

  function renderManager() {
    const t = team();
    $("#status-team").textContent = t.name;
    $("#status-list").replaceChildren(...t.statuses.map((s, i) => {
      const isDefault = s.kind !== "custom";
      const patch = (changes) => manage(() => api("PATCH", statusUrl(s), changes));
      const name = el("input", { value: s.name, maxlength: 40, "aria-label": `Name of ${s.name}`, disabled: isDefault,
        title: isDefault ? "Default statuses can't be renamed" : null });
      name.addEventListener("change", () => patch({ name: name.value }));
      const color = el("select", { class: "color-select", style: `--c: ${colorVar(s.color)}`, "aria-label": `Colour of ${s.name}` },
        COLORS.map((c) => el("option", { value: c }, label(c))));
      color.value = s.color;
      color.addEventListener("change", () => patch({ color: color.value }));
      const finished = el("input", { type: "checkbox", checked: s.finished, disabled: isDefault,
        title: isDefault ? "Fixed for default statuses" : null });
      finished.addEventListener("change", () => patch({ finished: finished.checked }));
      const reorder = (to) => {
        const ids = t.statuses.map((x) => x.id);
        [ids[i], ids[to]] = [ids[to], ids[i]];
        manage(() => api("PUT", `${statusUrl()}/order`, { ids }));
      };
      const row = el("li", { class: "status-row" },
        el("span", { class: "status-dot", style: `--c: ${colorVar(s.color)}` }),
        name, color,
        el("label", { class: "check finished-check" }, finished, "Counts as finished"),
        el("span", { class: "row-actions" },
          el("button", { type: "button", disabled: i === 0, "aria-label": `Move ${s.name} left`, title: "Move left", onclick: () => reorder(i - 1) }, "←"),
          el("button", { type: "button", disabled: i === t.statuses.length - 1, "aria-label": `Move ${s.name} right`, title: "Move right", onclick: () => reorder(i + 1) }, "→"),
          isDefault ? el("span", { class: "tag" }, "Default")
            : el("button", { type: "button", class: "danger", onclick: () => tryDelete(s, row) }, "Delete")));
      return row;
    }));
  }

  const addForm = $("#status-add");
  addForm.elements.color.replaceChildren(...COLORS.map((c) => el("option", { value: c }, label(c))));
  addForm.elements.color.value = "teal";
  addForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = addForm.elements;
    manage(async () => {
      await api("POST", statusUrl(), { name: f.name.value, color: f.color.value, finished: f.finished.checked });
      addForm.reset();
      f.color.value = "teal";
    });
  });
  $("#manage-statuses").addEventListener("click", () => { renderManager(); dialog.showModal(); });
  $("#status-close").addEventListener("click", () => dialog.close());

  startLive(me, {
    relevant: (ev) => ev.team_ids.includes(teamId()),
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
  fillSelect(f.priority, PRIORITIES);
  f.priority.value = "medium";
  const create = intent();

  async function syncProjects() {
    const n = await fillProjectOptions(f.project_id, f.team_id.value);
    if (n !== null) $("#project-field").hidden = n === 0;
  }

  async function syncTeam() {
    syncProjects();
    fillTypeOptions(f.type_id, f.team_id.value).then(syncTypeInput);
    const role = await fillAssignees(f.assignee_id, f.team_id.value, me);
    setTypeTyping(role === "lead" || me.user.is_admin);
    $("#assignee-field").hidden = role === null;
    $("#assignee-hint").textContent = role ? "You'll be able to reassign it later; the person you assign can't pass it on." : "";
    // Leads can always edit, so restricting is only offered to other creators.
    $("#restrict-field").hidden = role === "lead";
    if (role === "lead") f.restricted.checked = false;
    $("#team-hint").textContent = role === null
      ? "You're not in this team, so this becomes a request: they do the work, and you can follow, comment on and edit it under Requested by me."
      : "";
  }
  // Leads and admins get a type-ahead box instead of the plain list: pick a type, or type a new
  // name and press Enter to add it to the team. Everyone else just picks from the list.
  const typeInput = $("#type-input");
  let typing = false;
  const typeNames = () => [...f.type_id.options].map((o) => o.textContent);
  const typeNamed = (name) => [...f.type_id.options].find((o) => o.textContent.toLowerCase() === name.trim().toLowerCase());

  function setTypeTyping(on) {
    typing = on;
    f.type_id.hidden = on;
    f.type_id.required = !on;
    typeInput.hidden = !on;
    $("#type-hint").hidden = !on;
    syncTypeInput();
  }

  function syncTypeInput() {
    $("#type-options").replaceChildren(...typeNames().map((n) => el("option", { value: n })));
    typeInput.value = f.type_id.selectedOptions[0]?.textContent || "";
    updateTypeTools();
  }

  // Rename and Remove act on the chosen type, named on the buttons.
  function updateTypeTools() {
    const name = f.type_id.selectedOptions[0]?.textContent || "";
    $("#type-rename").textContent = name ? `Rename “${name}”` : "Rename";
    $("#type-remove").textContent = name ? `Remove “${name}”` : "Remove";
    $("#type-rename").disabled = $("#type-remove").disabled = !name;
  }

  $("#type-rename").addEventListener("click", async () => {
    const option = f.type_id.selectedOptions[0];
    if (!option) return;
    const name = await promptModal({ title: `Rename “${option.textContent}”`, message: "Its tasks keep the type; only the name changes.",
      input: { label: "New name", value: option.textContent, single: true }, confirmLabel: "Rename" });
    if (!name || name === option.textContent) return;
    try {
      const renamed = await api("PATCH", `/api/teams/${f.team_id.value}/types/${option.value}`, { name });
      option.textContent = renamed.name;
      syncTypeInput();
      showError($("#new-error"), null);
      flash(`Renamed to "${renamed.name}".`, true);
    } catch (err) {
      showError($("#new-error"), err);
    }
  });

  $("#type-remove").addEventListener("click", async () => {
    const option = f.type_id.selectedOptions[0];
    if (!option) return;
    const name = option.textContent;
    if (!(await confirmModal({ title: `Remove “${name}”?`, message: "It won't be available for new tasks.",
      confirmLabel: "Remove type", danger: true }))) return;
    const url = `/api/teams/${f.team_id.value}/types/${option.value}`;
    try {
      try {
        await api("DELETE", url);
      } catch (err) {
        if (err.code !== "TYPE_IN_USE") throw err;
        const n = err.details.task_count;
        const to = await askModal({
          title: `Move ${n} ${n === 1 ? "task" : "tasks"} to another type?`,
          message: `“${name}” is used by ${n} ${n === 1 ? "task" : "tasks"}. Choose the type they should get instead.`,
          choices: { label: "New type", options: [...f.type_id.options].filter((o) => o !== option).map((o) => ({ value: o.value, label: o.textContent })) },
          confirmLabel: "Move and remove", danger: true,
        });
        if (to === null) return;
        await api("DELETE", `${url}?move_to=${to}`);
      }
      await fillTypeOptions(f.type_id, f.team_id.value);
      syncTypeInput();
      showError($("#new-error"), null);
      flash(`Removed “${name}”.`, true);
    } catch (err) {
      showError($("#new-error"), err);
    }
  });

  // Make what's typed the chosen type, creating it first if the team doesn't have it. Returns false on failure.
  async function commitType() {
    const name = typeInput.value.trim();
    if (!typing || !name) return true;
    const existing = typeNamed(name);
    if (existing) { f.type_id.value = existing.value; typeInput.value = existing.textContent; return true; }
    try {
      const made = await api("POST", `/api/teams/${f.team_id.value}/types`, { name });
      f.type_id.append(el("option", { value: made.id }, made.name));
      f.type_id.value = made.id;
      syncTypeInput();
      flash(`"${made.name}" added as a task type for ${f.team_id.selectedOptions[0].textContent}.`, true);
      return true;
    } catch (err) {
      showError($("#new-error"), err);
      return false;
    }
  }

  // The list of suggestions only shows names matching the text, so empty the box while it has focus.
  typeInput.addEventListener("focus", () => { typeInput.dataset.was = typeInput.value; typeInput.value = ""; });
  typeInput.addEventListener("blur", () => { if (!typeInput.value.trim()) typeInput.value = typeInput.dataset.was || ""; });
  typeInput.addEventListener("input", () => { const t = typeNamed(typeInput.value); if (t) { f.type_id.value = t.value; updateTypeTools(); } });
  typeInput.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();  // Enter adds the type; it must not submit the form
    commitType();
  });
  f.team_id.addEventListener("change", syncTeam);
  await syncTeam();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!(await commitType())) return;
    const body = {
      team_id: f.team_id.value, title: f.title.value, description: f.description.value,
      type_id: f.type_id.value, priority: f.priority.value,
      project_id: $("#project-field").hidden ? null : f.project_id.value || null,
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
      // Older entries recorded an action ("completed"); newer ones just the two statuses.
      return [p.action ? `${PAST[p.action] || p.action} (${label(p.from)} → ${label(p.to)})`
        : `moved it from ${p.from} to ${p.to}`,
        p.reason ? el("div", { class: "reason" }, `Reason: ${p.reason}`) : null];
    case "approval":
      return [p.decision === "approved" ? "approved this task" : "rejected this task",
        p.reason ? el("div", { class: "reason" }, `Reason: ${p.reason}`) : null];
    case "updated": {
      const parts = Object.entries(p.changes || {}).map(([field, c]) => {
        if (field === "description") return "edited the description";
        if (field === "team") return `moved it from ${c.from} to ${c.to}`;
        if (field === "project") return c.to ? `${c.from ? `moved it from project “${c.from}” to “${c.to}”` : `added it to project “${c.to}”`}` : `removed it from project “${c.from}”`;
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
  project_id: it.project ? it.project.id : null, type_id: it.type.id,
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
      el("div", { class: "badges" }, statusBadge(item.status), prioBadge(item.priority), approvalTag(item),
        el("span", { class: "tag" }, item.type.name), el("span", { class: "tag" }, item.team.name),
        item.project ? projectTag(item.project) : null,
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
        el("dt", {}, "Project"), el("dd", {}, item.project ? item.project.name : "None"),
        el("dt", {}, "Assignee"), el("dd", {}, el("span", { class: "who" }, avatar(item.assignee), item.assignee ? item.assignee.name : "Unassigned")),
        item.assignee && item.assigned_by ? el("dt", {}, "Assigned by") : null,
        item.assignee && item.assigned_by ? el("dd", {}, item.assigned_by.name) : null,
        el("dt", {}, "Created by"), el("dd", {}, item.created_by.name),
        el("dt", {}, "Created"), el("dd", {}, fmtDate(item.created_at)),
        el("dt", {}, "Due"), el("dd", {}, dueChip(item) || "None"),
        el("dt", {}, "Approval"), el("dd", {}, !item.requires_approval ? "Not required"
          : { pending: "Waiting for a lead", approved: "Approved", rejected: "Rejected" }[item.approval] || "Required"),
        el("dt", {}, "Editing"), el("dd", {}, item.restricted ? "Leads and creator only" : "Anyone in the team"),
        el("dt", {}, "Last activity"), el("dd", {}, ago(item.last_activity_at)),
      ),
      el("div", { class: "actions" },
        can("claim") ? el("button", { type: "button", class: "primary", onclick: (ev) => claim(ev.target) }, "Take this task") : null,
        can("edit") && !editing ? el("button", { type: "button", onclick: openEditor }, "Edit") : null,
        can("approve") ? el("button", { type: "button", class: "primary", onclick: (ev) => decide("approve", ev.target) }, "Approve") : null,
        can("reject") ? el("button", { type: "button", class: "danger", onclick: (ev) => decide("reject", ev.target) }, "Reject") : null),
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
      const color = e.kind === "status_changed" && e.payload.to_color ? `--c: ${colorVar(e.payload.to_color)}` : null;
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

  // A lead (never the task's creator) approves or rejects a task that needs approval.
  async function decide(decision, button) {
    let reason = null;
    if (decision === "reject") {
      reason = await promptModal({ title: "Reject this task?", message: "Tell the team why (optional).",
        input: { label: "Reason" }, confirmLabel: "Reject", danger: true });
      if (reason === null) return;
    }
    await busy(button, async () => {
      try {
        item = await api("POST", `/api/items/${id}/approval`,
          { decision, reason: reason || null, expected_version: item.version }, { idempotencyKey: newKey() });
        flash(decision === "approve" ? "Task approved. Its status can change now." : "Task rejected.", true);
      } catch (err) {
        flash(err.code === "STALE_VERSION" ? "This task changed since you loaded it. Refresh and try again." : err.message);
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
    const typeSelect = el("select", { name: "type_id" });
    const projectSelect = el("select", { name: "project_id" });
    const projectField = el("label", {}, "Project", projectSelect,
      el("span", { class: "hint" }, "Only projects assigned to the task's team can be chosen. Moving it to a team that isn't on its project removes the project."));
    const assigneeSelect = el("select", { name: "assignee_id" });
    const assigneeField = el("label", {}, "Assign to", assigneeSelect,
      el("span", { class: "hint" }, "Leads and whoever assigned the task can reassign it. The person it's assigned to can't pass it on."));
    const form = el("form", { class: "card stack", id: "edit-form" },
      el("label", {}, "Title", el("input", { name: "title", value: item.title, maxlength: 200, required: true })),
      el("label", {}, "Description", el("textarea", { name: "description", rows: 8, maxlength: 20000 })),
      el("div", { class: "row" },
        el("label", {}, "Priority", el("select", { name: "priority" }, PRIORITIES.map((p) => el("option", { value: p }, label(p))))),
        el("label", {}, "Type", typeSelect),
        el("label", {}, "Due", el("input", { name: "due_at", type: "datetime-local", value: toLocalInput(item.due_at) }))),
      el("label", {}, "Team", teamSelect,
        el("span", { class: "hint" }, "Moving it to another team hands the work over. If the assignee isn't in that team, it becomes unassigned.")),
      projectField,
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
    async function syncProjects() {
      const n = await fillProjectOptions(projectSelect, teamSelect.value, item.project ? item.project.id : null);
      if (n !== null) projectField.hidden = n === 0 && !item.project;
    }
    const syncTypes = () => fillTypeOptions(typeSelect, teamSelect.value, { currentId: item.type.id, preferName: item.type.name });
    teamSelect.addEventListener("change", () => { syncAssignees(); syncProjects(); syncTypes(); });
    await Promise.all([syncAssignees(), syncProjects(), syncTypes()]);

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
    if (f.type_id.value) out.type_id = f.type_id.value;
    if (!f.project_id.closest("label").hidden) out.project_id = f.project_id.value || null;
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
    if (field === "type_id") {
      const option = [...document.querySelectorAll("#edit-form [name=type_id] option")].find((o) => o.value === value);
      return option ? option.textContent : current.type.name;
    }
    if (field === "project_id") {
      const option = [...document.querySelectorAll("#edit-form [name=project_id] option")].find((o) => o.value === value);
      return option ? option.textContent : current.project?.name || "Another project";
    }
    if (field === "restricted") return value ? "Leads and creator only" : "Anyone in the team";
    if (field === "due_at") return new Date(value).toLocaleString();
    return String(value);
  };
  const FIELD_NAMES = { team_id: "Team", project_id: "Project", type_id: "Type", assignee_id: "Assignee", restricted: "Editing", due_at: "Due" };

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
    relevant: (ev) => ev.item_id === id || (ev.kind === "statuses" && item && ev.team_ids.includes(item.team.id)),
    describe: () => "Someone else just updated this task.",
    onRefresh: async () => {
      const fresh = await api("GET", "/api/me"); // the team's statuses may have changed too
      me.teams = fresh.teams;
      setStatuses(me);
      await refresh();
    },
  });
  await refresh();
}

// ------------------------------------------------------------- profile ----

async function initProfile() {
  const me = await topbar("profile");
  flashPending();

  function statTile(count, text, href) {
    const tag = href ? "a" : "div";
    return el(tag, { class: `tile stat${href ? " link" : ""}`, href }, el("span", { class: "count" }, count), el("span", { class: "tile-label" }, text));
  }

  function render(p) {
    $("#profile-name").textContent = p.user.name;
    $("#profile-email").textContent = p.user.email;
    $("#profile-avatar").replaceChildren(avatar(p.user));
    $("#profile-teams-count").textContent =
      `${p.user.is_admin ? "Admin · " : ""}Member of ${p.teams.length} ${p.teams.length === 1 ? "team" : "teams"}`;
    $("#name-form").elements.name.value = p.user.name;

    $("#stats").replaceChildren(
      statTile(p.stats.assigned, "Assigned to you", "/?view=mine"),
      statTile(p.stats.overdue, "Overdue", "/?view=overdue&assignee=me"),
      statTile(p.stats.finished, "Finished by you"),
      statTile(p.stats.created, "Created by you"));

    $("#teams").replaceChildren(...p.teams.map((t) => el("article", { class: "card team-card" },
      el("div", { class: "team-head" },
        el("h3", {}, t.name),
        el("span", { class: `tag role-${t.role}` }, t.role === "lead" ? "Lead" : "Member")),
      el("p", { class: "muted" },
        `${t.member_count} ${t.member_count === 1 ? "member" : "members"} · ${t.open_tasks} open of ${t.total_tasks} ${t.total_tasks === 1 ? "task" : "tasks"}`),
      el("ul", { class: "member-list" }, t.members.map((m) => el("li", {},
        avatar(m), el("span", {}, m.id === p.user.id ? `${m.name} (you)` : m.name),
        m.role === "lead" ? el("span", { class: "tag" }, "Lead") : null))),
      el("a", { href: `/board.html?team_id=${t.id}` }, "Open this team's board"))));
  }

  async function load() {
    try {
      render(await api("GET", "/api/profile"));
      showError($("#profile-error"), null);
      $("#profile-body").hidden = false;
    } catch (err) {
      showError($("#profile-error"), err);
    }
  }

  const nameForm = $("#name-form");
  nameForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await busy(nameForm.querySelector("button"), async () => {
      try {
        const p = await api("PATCH", "/api/profile", { name: nameForm.elements.name.value });
        render(p);
        topbar("profile");
        showError($("#name-error"), null);
        flash("Name updated.", true);
      } catch (err) {
        showError($("#name-error"), err.code === "VALIDATION_FAILED" ? new Error("Enter a name (up to 100 characters).") : err);
      }
    });
  });

  const pwForm = $("#password-form");
  pwForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = pwForm.elements;
    if (f.new_password.value !== f.confirm.value) {
      showError($("#password-error"), new Error("The new passwords don't match."));
      return;
    }
    await busy(pwForm.querySelector("button"), async () => {
      try {
        await api("POST", "/api/profile/password", { current_password: f.current_password.value, new_password: f.new_password.value });
        pwForm.reset();
        showError($("#password-error"), null);
        flash("Password changed.", true);
      } catch (err) {
        showError($("#password-error"), err.code === "VALIDATION_FAILED" ? new Error("The new password needs at least 8 characters.") : err);
      }
    });
  });

  startLive(me, { relevant: () => false, describe: () => "", onRefresh: load });
  await load();
}

// --------------------------------------------------------------- boot ----

const pages = {
  login: initLogin, setup: initSetup, "change-password": initChangePassword, overview: initOverview,
  board: initBoard, new: initNew, item: initItem,
  admin: () => initAdmin({ api, el, $, topbar, flash, showError, busy, label, avatar, newKey, setRefresh, confirmModal }),
  profile: initProfile,
};
const init = pages[document.body.dataset.page];
if (init) init().catch((err) => console.error(err));
