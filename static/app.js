// Newtonite Ops frontend. Plain JS, no build step.
// Server state is fetched per page; the URL holds filter state; every
// mutation carries an Idempotency-Key so a retry can never act twice.

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
const label = (s) => (s || "").replaceAll("_", " ");
const STATUSES = ["awaiting_approval", "open", "in_progress", "blocked", "done", "cancelled"];
const PRIORITIES = ["urgent", "high", "medium", "low"];
const TYPES = ["customer_issue", "engineering", "payment", "incident", "compliance", "task"];
const TRANSITIONS = {
  approve: "Approve", reject: "Reject", start: "Start work", block: "Block",
  unblock: "Unblock", complete: "Complete", reopen: "Reopen", cancel: "Cancel item",
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
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const statusBadge = (s) => el("span", { class: `badge status-${s}` }, label(s));
const prioBadge = (p) => el("span", { class: `badge prio-${p}` }, p);

function fillSelect(select, values, { keep = true } = {}) {
  if (!keep) select.replaceChildren();
  for (const v of values) select.append(el("option", { value: v }, label(v)));
}

function showError(node, err) {
  node.textContent = err ? err.message : "";
  node.hidden = !err;
}

let flashTimer;
function flash(message, ok = false) {
  const box = $("#flash");
  box.textContent = message;
  box.className = ok ? "flash ok" : "flash";
  box.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

async function topbar() {
  const me = await api("GET", "/api/me");
  $("#topbar").replaceChildren(
    el("a", { class: "brand", href: "/" }, "Newtonite Ops"),
    el("a", { href: "/" }, "Overview"),
    el("a", { href: "/new.html" }, "New item"),
    el("span", { class: "spacer" }),
    el("span", { class: "muted" }, me.user.name),
    el("button", { type: "button", onclick: async () => { await api("POST", "/api/logout"); location.href = "/login.html"; } }, "Log out"),
  );
  return me;
}

// -------------------------------------------------------------- login ----

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

// ------------------------------------------------------------- signup ----

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
          ? "Please check the form: a valid email, a password of at least 8 characters, and a team."
          : err.message;
        showError($("#signup-error"), new Error(msg));
      }
    });
  });
}

// ----------------------------------------------------------- overview ----

const VIEW_LABELS = {
  mine: "Assigned to me",
  unassigned_urgent: "Unassigned urgent / high",
  awaiting_my_approval: "Awaiting my approval",
  overdue: "Overdue",
  stale: "No activity for 3+ days",
};

async function initOverview() {
  const me = await topbar();
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

  async function loadTiles() {
    const { counts } = await api("GET", "/api/overview");
    const active = params().get("view");
    $("#tiles").replaceChildren(...Object.entries(VIEW_LABELS).map(([view, text]) =>
      el("button", {
        type: "button", class: `tile${active === view ? " active" : ""}`,
        onclick: () => setParams({ view: active === view ? "" : view }),
      }, el("span", { class: "count" }, counts[view]), text)));
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
      $("#rows").replaceChildren(...data.items.map((it) => el("tr", {},
        el("td", { class: "title" }, el("a", { href: `/item.html?id=${it.id}` }, it.title)),
        el("td", {}, it.team.name),
        el("td", {}, statusBadge(it.status)),
        el("td", {}, prioBadge(it.priority)),
        el("td", {}, it.assignee ? it.assignee.name : el("span", { class: "muted" }, "Unassigned")),
        el("td", { title: fmtDate(it.last_activity_at) }, ago(it.last_activity_at)),
      )));
      $("#empty").hidden = data.items.length > 0;
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
  load();
}

// ----------------------------------------------------------- new item ----

async function initNew() {
  const me = await topbar();
  const form = $("#new-form");
  for (const t of me.teams) form.elements.team_id.append(el("option", { value: t.id }, t.name));
  fillSelect(form.elements.type, TYPES);
  fillSelect(form.elements.priority, PRIORITIES);
  form.elements.priority.value = "medium";
  const create = intent();

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    const body = {
      team_id: f.get("team_id"), title: f.get("title"), description: f.get("description"),
      type: f.get("type"), priority: f.get("priority"),
      requires_approval: f.get("requires_approval") === "on",
      due_at: f.get("due_at") ? new Date(f.get("due_at")).toISOString() : null,
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

// -------------------------------------------------------------- item ----

function describe(entry) {
  const p = entry.payload || {};
  switch (entry.kind) {
    case "created": return ["created this item"];
    case "comment": return ["commented", el("div", { class: "comment" }, p.body)];
    case "assigned":
      if (p.to && !p.from && p.to === entry.actor.id) return ["claimed this item"];
      if (p.to && p.from) return [`reassigned from ${p.from_name} to ${p.to_name}`];
      if (p.to) return [`assigned it to ${p.to_name}`];
      return [`unassigned ${p.from_name || "the previous owner"}`];
    case "status_changed":
      return [`${PAST[p.action] || p.action} (${label(p.from)} → ${label(p.to)})`,
        p.reason ? el("div", { class: "reason" }, `Reason: ${p.reason}`) : null];
    case "updated": {
      const parts = Object.entries(p.changes || {}).map(([field, c]) =>
        field === "description" ? "edited the description"
          : `changed ${label(field)} from “${c.from ?? "none"}” to “${c.to ?? "none"}”`);
      return [parts.join(", ") || "updated the item"];
    }
    default: return [entry.kind];
  }
}

async function initItem() {
  const me = await topbar();
  const id = new URLSearchParams(location.search).get("id");
  let item = null;       // latest server state
  let editing = null;    // { baseVersion } while the edit form is open
  let members = null;    // team members for the assign picker (loaded once)
  let olderCursor = null;

  async function refresh() {
    try {
      const fresh = await api("GET", `/api/items/${encodeURIComponent(id)}`);
      // An older copy (a poll that raced a save) must never replace newer state.
      if (latest(item, fresh) !== fresh) return;
      const changed = !item || fresh.version !== item.version || fresh.last_activity_at !== item.last_activity_at;
      item = fresh;
      $("#item-root").hidden = false;
      showError($("#load-error"), null);
      render();
      if (editing && item.version > editing.baseVersion) {
        $("#banner").hidden = false;
        $("#banner").textContent = "This item changed while you were editing. Saving will show you both versions.";
      }
      if (changed) loadTimeline();
    } catch (err) {
      if (err.status === 404) {
        $("#item-root").hidden = true;
        showError($("#load-error"), new Error("This work item doesn't exist, or you don't have access to it."));
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
        el("span", { class: "badge" }, label(item.type)), el("span", { class: "badge" }, item.team.name)),
    );
    $("#description").replaceChildren(item.description
      ? document.createTextNode(item.description)
      : el("span", { class: "muted" }, "No description."));
    renderMeta();
    $("#comment-form").hidden = !item.allowed_actions.includes("comment");
  }

  function renderMeta() {
    const can = (a) => item.allowed_actions.includes(a);
    const assignee = item.assignee ? item.assignee.name : el("span", { class: "muted" }, "Unassigned");
    const actions = Object.keys(TRANSITIONS).filter(can).map((a) =>
      el("button", { type: "button", class: a === "cancel" || a === "reject" ? "danger" : "", onclick: (ev) => transition(a, ev.target) }, TRANSITIONS[a]));

    const assignPicker = can("assign") ? el("select", { "aria-label": "Assign to", onchange: (ev) => assign(ev.target) },
      el("option", { value: "" }, "Change assignee…")) : null;
    if (assignPicker) loadMembers().then((list) => {
      for (const m of list) assignPicker.append(el("option", { value: m.id }, m.name));
      if (item.assignee) assignPicker.append(el("option", { value: "none" }, "— Unassign —"));
    });

    $("#meta").replaceChildren(
      el("dl", {},
        el("dt", {}, "Assignee"), el("dd", {}, assignee),
        el("dt", {}, "Created by"), el("dd", {}, item.created_by.name),
        el("dt", {}, "Created"), el("dd", {}, fmtDate(item.created_at)),
        el("dt", {}, "Due"), el("dd", {}, fmtDate(item.due_at)),
        el("dt", {}, "Approval"), el("dd", {}, item.requires_approval ? "Required" : "Not required"),
        el("dt", {}, "Last activity"), el("dd", {}, ago(item.last_activity_at)),
      ),
      el("div", { class: "actions" },
        can("claim") ? el("button", { type: "button", class: "primary", onclick: (ev) => claim(ev.target) }, "Claim") : null,
        can("edit") && !editing ? el("button", { type: "button", onclick: openEditor }, "Edit") : null,
        ...actions),
      ...(assignPicker ? [el("div", { class: "actions" }, assignPicker)] : []),
    );
  }

  async function loadMembers() {
    members ??= await api("GET", `/api/teams/${item.team.id}/members`);
    return members;
  }

  async function loadTimeline(older = false) {
    const q = older && olderCursor ? `?before_id=${olderCursor}` : "";
    const page = await api("GET", `/api/items/${encodeURIComponent(id)}/activity${q}`);
    const rows = page.entries.map((e) => el("li", {},
      el("strong", {}, e.actor.name), " ", ...describe(e).slice(0, 1),
      el("span", { class: "when", title: fmtDate(e.created_at) }, ago(e.created_at)),
      ...describe(e).slice(1)));
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
        flash("You now own this item.", true);
      } catch (err) {
        claimIntent.settle(err);
        item = before;
        if (err.code === "ALREADY_CLAIMED") {
          item = { ...before, assignee: err.details.assignee };
          flash(`Already claimed by ${err.details.assignee ? err.details.assignee.name : "someone else"}.`);
        } else {
          flash(err.message);
        }
        render();
      }
      await refresh();
    });
  }

  const actionIntents = {};
  async function transition(action, button) {
    let reason = null;
    if (REASON_REQUIRED.has(action)) {
      reason = window.prompt(`Why? (required to ${action})`);
      if (reason === null) return;
      if (!reason.trim()) { flash("A reason is required."); return; }
    }
    const it = (actionIntents[action] ??= intent());
    await busy(button, async () => {
      try {
        item = await api("POST", `/api/items/${id}/transitions`,
          { action, reason, expected_version: item.version }, { idempotencyKey: it.key() });
        it.settle(null);
        flash(`Item ${PAST[action]}.`, true);
      } catch (err) {
        it.settle(err);
        flash(err.code === "STALE_VERSION"
          ? "This item changed since you loaded it. The latest version is shown — check it and try again."
          : err.message);
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
      flash(err.code === "STALE_VERSION" ? "This item changed since you loaded it — try again." : err.message);
    }
    await refresh();
  }

  // ---- editing with stale-write protection ----
  const toLocalInput = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  };

  function openEditor() {
    editing = { baseVersion: item.version };
    const form = el("form", { class: "card stack", id: "edit-form" },
      el("label", {}, "Title", el("input", { name: "title", value: item.title, maxlength: 200, required: true })),
      el("label", {}, "Description", el("textarea", { name: "description", rows: 8, maxlength: 20000 })),
      el("div", { class: "row" },
        el("label", {}, "Priority", el("select", { name: "priority" }, PRIORITIES.map((p) => el("option", { value: p }, p)))),
        el("label", {}, "Due", el("input", { name: "due_at", type: "datetime-local", value: toLocalInput(item.due_at) }))),
      el("div", { id: "conflict" }),
      el("div", { class: "row" },
        el("button", { type: "submit", class: "primary" }, "Save"),
        el("button", { type: "button", onclick: closeEditor }, "Cancel")),
    );
    form.elements.description.value = item.description;
    form.elements.priority.value = item.priority;
    form.addEventListener("submit", (ev) => { ev.preventDefault(); save(editing.baseVersion); });
    $("#editor").replaceChildren(form);
    $("#editor").hidden = false;
    editing.snapshot = draft(); // what the form showed when editing began
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
    const form = $("#edit-form");
    return {
      title: form.elements.title.value.trim(),
      description: form.elements.description.value,
      priority: form.elements.priority.value,
      due_at: form.elements.due_at.value ? new Date(form.elements.due_at.value).toISOString() : null,
    };
  }

  async function save(expectedVersion) {
    const button = $("#edit-form button[type=submit]");
    await busy(button, async () => {
      const changes = changedFields(editing.snapshot, draft());
      if (Object.keys(changes).length === 0) { closeEditor(); flash("Nothing to save.", true); return; }
      try {
        item = await api("PATCH", `/api/items/${id}`, { expected_version: expectedVersion, ...changes });
        closeEditor();
        flash("Saved.", true);
        loadTimeline();
      } catch (err) {
        if (err.code === "STALE_VERSION") showConflict(err.details);
        else flash(err.message);
      }
    });
  }

  // Keep the user's draft; show it next to what is on the server now.
  function showConflict({ current, changed_by }) {
    item = current;
    renderMeta();
    // Only fields I changed matter; everything else keeps their version.
    const rows = conflictRows(changedFields(editing.snapshot, draft()), current).map((r) =>
      el("tr", {}, el("th", {}, label(r.field)), el("td", {}, String(r.mine ?? "—")), el("td", {}, String(r.current ?? "—"))));
    $("#banner").hidden = true;
    $("#conflict").replaceChildren(el("div", { class: "conflict" },
      el("strong", {}, changed_by && changed_by.id === me.user.id
        ? "You changed this item somewhere else (another tab or window) while this editor was open."
        : `${changed_by ? changed_by.name : "Someone"} changed this item while you were editing.`),
      rows.length
        ? el("table", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Yours"), el("th", {}, "Current")), rows)
        : el("p", {}, "Your edits don't conflict with theirs."),
      el("div", { class: "row" },
        el("button", { type: "button", class: "primary", onclick: () => { editing.baseVersion = current.version; save(current.version); } }, "Keep mine"),
        el("button", { type: "button", onclick: () => { closeEditor(); refresh(); } }, "Discard mine")),
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

  await refresh();
  // Keep the view fresh: poll, and re-check when the tab comes back.
  setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  window.addEventListener("focus", refresh);
}

// --------------------------------------------------------------- boot ----

const pages = { login: initLogin, signup: initSignup, overview: initOverview, new: initNew, item: initItem };
const init = pages[document.body.dataset.page];
if (init) init().catch((err) => console.error(err));
