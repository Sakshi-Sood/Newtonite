// Admin dashboard: teams, users and membership. Receives the shared helpers from app.js
// (so this file imports nothing). Every endpoint it calls is admin-only on the server.

const PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no look-alikes (0/O, 1/l/I)

function generatePassword(length = 12) {
  const bytes = crypto.getRandomValues(new Uint8Array(length));
  return Array.from(bytes, (b) => PASSWORD_ALPHABET[b % PASSWORD_ALPHABET.length]).join("");
}

export async function initAdmin(ctx) {
  const { api, el, $, topbar, flash, showError, busy, avatar, setRefresh } = ctx;
  const me = await topbar("admin");
  if (!me.user.is_admin) {
    $("#admin-denied").hidden = false;
    return;
  }
  $("#admin-root").hidden = false;

  // Friendlier text for the one error shape the form checks can't explain on their own.
  const messageOf = (err) => (err.code === "VALIDATION_FAILED"
    ? "Check the fields: names can't be empty, emails must be valid and passwords need at least 8 characters."
    : err.message);
  const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const roleName = (r) => (r === "lead" ? "Lead" : "Member");
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  // ------------------------------------------------------------------ dialog
  const dlg = $("#admin-dialog");
  let onDialogClose = () => {};
  function openDialog(title, body, { onClose } = {}) {
    $("#admin-dialog-title").textContent = title;
    $("#admin-dialog-body").replaceChildren(...[].concat(body));
    onDialogClose = onClose || (() => {});
    if (!dlg.open) dlg.showModal();
  }
  dlg.addEventListener("close", () => { onDialogClose(); loadSummary(); loadTeams(true); loadUsers(true); });
  $("#admin-dialog-close").addEventListener("click", () => dlg.close());
  const dialogError = () => el("p", { class: "error", hidden: true, role: "alert" });

  // ----------------------------------------------------------------- summary
  async function loadSummary() {
    try {
      const s = await api("GET", "/api/admin/summary");
      $("#admin-summary").textContent =
        `${plural(s.teams, "team", "teams")} · ${plural(s.active_users, "active user", "active users")} · ${plural(s.admins, "admin", "admins")}`;
    } catch { /* the lists below show their own errors */ }
  }

  // ------------------------------------------------------------------- tabs
  function showTab(name) {
    for (const t of ["teams", "users"]) {
      const on = t === name;
      $(`#tab-${t}`).setAttribute("aria-selected", String(on));
      $(`#panel-${t}`).hidden = !on;
    }
    history.replaceState(null, "", name === "users" ? "#users" : location.pathname);
  }
  $("#tab-teams").addEventListener("click", () => showTab("teams"));
  $("#tab-users").addEventListener("click", () => showTab("users"));
  if (location.hash === "#users") showTab("users");

  // ------------------------------------------------------------------- teams
  const teamsState = { q: "", page: 1, rows: [], more: false };

  async function loadTeams(reset) {
    if (reset) teamsState.page = 1;
    const pages = reset ? Math.max(1, Math.ceil(teamsState.rows.length / 50)) : 1; // reload everything already shown
    try {
      let rows = [], more = false;
      for (let p = 1; p <= pages; p++) {
        const page = reset ? p : teamsState.page;
        const data = await api("GET", `/api/admin/teams?q=${encodeURIComponent(teamsState.q)}&page=${page}&page_size=50`);
        rows = rows.concat(data.items);
        more = data.has_more;
      }
      teamsState.rows = reset ? rows : teamsState.rows.concat(rows);
      teamsState.more = more;
      renderTeams();
      showError($("#teams-error"), null);
    } catch (err) {
      showError($("#teams-error"), err);
    }
  }

  function renderTeams() {
    $("#teams-rows").replaceChildren(...teamsState.rows.map((t) => el("tr", {},
      el("td", { class: "title" }, t.name),
      el("td", {}, t.lead_names || el("span", { class: "warn-text" }, t.member_count ? "No lead" : "Empty")),
      el("td", {}, t.member_count),
      el("td", {}, `${t.open_tasks} of ${t.total_tasks}`),
      el("td", { class: "row-actions" },
        el("button", { type: "button", onclick: () => teamMembersDialog(t) }, "Members"),
        el("button", { type: "button", onclick: () => renameTeamDialog(t) }, "Rename"),
        el("button", { type: "button", class: "danger", onclick: () => deleteTeamDialog(t) }, "Delete")))));
    $("#teams-empty").hidden = teamsState.rows.length > 0;
    $("#teams-more").hidden = !teamsState.more;
  }

  $("#team-q").addEventListener("input", debounce((ev) => { teamsState.q = ev.target.value.trim(); loadTeams(true); }));
  $("#teams-more").addEventListener("click", () => { teamsState.page += 1; loadTeams(false); });
  const createForm = $("#team-create");
  createForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    await busy(createForm.querySelector("button"), async () => {
      try {
        const t = await api("POST", "/api/admin/teams", { name: createForm.elements.name.value });
        createForm.reset();
        flash(`Team "${t.name}" created with the default statuses. Add its first lead under Members.`, true);
        await Promise.all([loadTeams(true), loadSummary()]);
      } catch (err) {
        showError($("#teams-error"), new Error(messageOf(err)));
      }
    });
  });

  function renameTeamDialog(team) {
    const err = dialogError();
    const form = el("form", { class: "stack" },
      el("label", {}, "Team name", el("input", { name: "name", value: team.name, maxlength: 100, required: true })),
      err, el("div", {}, el("button", { type: "submit", class: "primary" }, "Save name")));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      await busy(form.querySelector("button"), async () => {
        try {
          await api("PATCH", `/api/admin/teams/${team.id}`, { name: form.elements.name.value });
          flash("Team renamed.", true);
          dlg.close();
        } catch (e) { showError(err, new Error(messageOf(e))); }
      });
    });
    openDialog(`Rename ${team.name}`, form);
  }

  function deleteTeamDialog(team) {
    const err = dialogError();
    const confirmInput = el("input", { name: "confirm", autocomplete: "off", "aria-label": "Type the team name to confirm" });
    const button = el("button", { type: "submit", class: "danger", disabled: true }, "Delete team for good");
    confirmInput.addEventListener("input", () => { button.disabled = confirmInput.value.trim() !== team.name; });
    const form = el("form", { class: "stack" },
      el("p", {}, `This permanently deletes "${team.name}" with its ${plural(team.total_tasks, "task", "tasks")}, their history and its statuses. `,
        `The ${plural(team.member_count, "member", "members")} stay in the app but lose access to this team. This can't be undone.`),
      el("label", {}, `Type "${team.name}" to confirm`, confirmInput), err, el("div", {}, button));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      await busy(button, async () => {
        try {
          await api("DELETE", `/api/admin/teams/${team.id}`, { confirm_name: confirmInput.value });
          flash(`Team "${team.name}" deleted.`, true);
          dlg.close();
        } catch (e) { showError(err, new Error(messageOf(e))); }
      });
    });
    openDialog(`Delete ${team.name}?`, form);
  }

  // A team's members: change roles, remove people, add anyone.
  async function teamMembersDialog(team) {
    const body = el("div", { class: "stack" });
    const err = dialogError();
    openDialog(`Members of ${team.name}`, [body]);

    async function render() {
      let members;
      try { members = await api("GET", `/api/admin/teams/${team.id}/members`); } catch (e) { showError(err, e); return; }
      const mutate = async (fn) => {
        try { await fn(); showError(err, null); await render(); } catch (e) { showError(err, new Error(messageOf(e))); }
      };
      const rows = members.map((m) => {
        const role = el("select", { "aria-label": `Role of ${m.name}` },
          el("option", { value: "member" }, "Member"), el("option", { value: "lead" }, "Lead"));
        role.value = m.role;
        role.addEventListener("change", () => mutate(async () => {
          try { await api("PUT", `/api/admin/users/${m.id}/teams/${team.id}`, { role: role.value }); }
          catch (e) { role.value = m.role; throw e; }
        }));
        return el("li", { class: "member-row" },
          avatar({ id: m.id, name: m.name }),
          el("span", { class: "who-block" }, el("strong", {}, m.name), m.active ? null : el("span", { class: "tag" }, " Deactivated"), el("br"),
            el("span", { class: "muted" }, m.email)),
          role,
          el("button", { type: "button", class: "danger", onclick: () => {
            if (!window.confirm(`Remove ${m.name} from ${team.name}? Their tasks in this team become unassigned.`)) return;
            mutate(() => api("DELETE", `/api/admin/users/${m.id}/teams/${team.id}`));
          } }, "Remove"));
      });

      // Add someone: search by name or email, then pick.
      const search = el("input", { type: "search", placeholder: "Find a user to add", "aria-label": "Find a user to add" });
      const pick = el("select", { "aria-label": "User to add" }, el("option", { value: "" }, "Search above, then choose…"));
      const newRole = el("select", { "aria-label": "Role" }, el("option", { value: "member" }, "Member"), el("option", { value: "lead" }, "Lead"));
      const add = el("button", { type: "button", class: "primary", disabled: true }, "Add to team");
      const inTeam = new Set(members.map((m) => m.id));
      const find = debounce(async () => {
        try {
          const data = await api("GET", `/api/admin/users?q=${encodeURIComponent(search.value.trim())}&page_size=20`);
          const found = data.items.filter((u) => u.active && !inTeam.has(u.id));
          pick.replaceChildren(el("option", { value: "" }, found.length ? "Choose a user…" : "No matching users to add"),
            ...found.map((u) => el("option", { value: u.id }, `${u.name} (${u.email})`)));
          add.disabled = true;
        } catch (e) { showError(err, e); }
      }, 250);
      search.addEventListener("input", find);
      pick.addEventListener("change", () => { add.disabled = !pick.value; });
      add.addEventListener("click", () => busy(add, () => mutate(async () => {
        await api("PUT", `/api/admin/users/${pick.value}/teams/${team.id}`, { role: newRole.value });
      })));

      body.replaceChildren(
        members.length ? el("ul", { class: "member-list-admin" }, rows)
          : el("p", { class: "muted" }, "No members yet. Add this team's first lead below."),
        el("h3", {}, "Add a member"),
        el("div", { class: "row add-row" }, search, pick, newRole, add),
        el("p", { class: "hint" }, "A team with members always needs at least one lead, so add a lead first."),
        err);
      find();
    }
    await render();
  }

  // ------------------------------------------------------------------- users
  const usersState = { q: "", page: 1, rows: [], more: false };

  async function loadUsers(reset) {
    if (reset) usersState.page = 1;
    const pages = reset ? Math.max(1, Math.ceil(usersState.rows.length / 25)) : 1;
    try {
      let rows = [], more = false;
      for (let p = 1; p <= pages; p++) {
        const page = reset ? p : usersState.page;
        const data = await api("GET", `/api/admin/users?q=${encodeURIComponent(usersState.q)}&page=${page}&page_size=25`);
        rows = rows.concat(data.items);
        more = data.has_more;
      }
      usersState.rows = reset ? rows : usersState.rows.concat(rows);
      usersState.more = more;
      renderUsers();
      showError($("#users-error"), null);
    } catch (err) {
      showError($("#users-error"), err);
    }
  }

  function renderUsers() {
    $("#users-rows").replaceChildren(...usersState.rows.map((u) => el("tr", { class: u.active ? "" : "inactive-row" },
      el("td", {}, el("span", { class: "who" }, avatar({ id: u.id, name: u.name }),
        el("span", {}, el("strong", {}, u.name), el("br"), el("span", { class: "muted" }, u.email)))),
      el("td", {}, u.teams.length ? el("span", { class: "chip-list" }, u.teams.map((t) =>
        el("span", { class: `tag${t.role === "lead" ? " role-lead" : ""}` }, `${t.name} · ${roleName(t.role)}`)))
        : el("span", { class: "muted" }, "No teams")),
      el("td", {}, el("span", { class: "chip-list" },
        u.is_admin ? el("span", { class: "tag role-lead" }, "Admin") : null,
        u.active ? null : el("span", { class: "tag approval-rejected" }, "Deactivated"),
        u.must_change_password ? el("span", { class: "tag", title: "Still has a temporary password" }, "Temporary password") : null)),
      el("td", { class: "row-actions" }, el("button", { type: "button", onclick: () => editUserDialog(u.id) }, "Edit")))));
    $("#users-empty").hidden = usersState.rows.length > 0;
    $("#users-more").hidden = !usersState.more;
  }

  $("#user-q").addEventListener("input", debounce((ev) => { usersState.q = ev.target.value.trim(); loadUsers(true); }));
  $("#users-more").addEventListener("click", () => { usersState.page += 1; loadUsers(false); });
  $("#user-new").addEventListener("click", () => newUserDialog());

  // A password field with a "Generate" helper (shown in plain text on purpose: the admin has to pass it on).
  function passwordField(label) {
    const input = el("input", { name: "password", type: "text", autocomplete: "off", minlength: 8, maxlength: 200, required: true,
      spellcheck: "false" });
    const gen = el("button", { type: "button", onclick: () => { input.value = generatePassword(); input.focus(); } }, "Generate");
    return { input, node: el("label", {}, label, el("span", { class: "row inline" }, input, gen),
      el("span", { class: "hint" }, "Temporary: they must choose their own password the first time they sign in.")) };
  }

  async function newUserDialog() {
    let options;
    try { options = await api("GET", "/api/admin/teams/options"); } catch (e) { flash(e.message); return; }
    const err = dialogError();
    const pw = passwordField("Temporary password");
    const teamRows = [];
    const teamsBox = el("div", { class: "stack" });
    const addTeamRow = () => {
      const team = el("select", { "aria-label": "Team" }, options.map((t) => el("option", { value: t.id }, t.name)));
      const role = el("select", { "aria-label": "Role in team" }, el("option", { value: "member" }, "Member"), el("option", { value: "lead" }, "Lead"));
      // Keep the dropdowns in a plain object: `role` is a built-in property of DOM elements.
      const entry = { team, role };
      const row = el("div", { class: "row add-row" }, team, role,
        el("button", { type: "button", onclick: () => { teamRows.splice(teamRows.indexOf(entry), 1); row.remove(); } }, "Remove"));
      teamRows.push(entry);
      teamsBox.append(row);
    };
    const form = el("form", { class: "stack" },
      el("div", { class: "row" },
        el("label", {}, "Full name", el("input", { name: "name", maxlength: 100, required: true, autocomplete: "off" })),
        el("label", {}, "Email", el("input", { name: "email", type: "email", maxlength: 254, required: true, autocomplete: "off" }))),
      pw.node,
      el("label", { class: "check" }, el("input", { name: "is_admin", type: "checkbox" }), "Make this person an admin"),
      el("fieldset", { class: "stack" }, el("legend", {}, "Teams"),
        options.length ? teamsBox : el("p", { class: "muted" }, "No teams exist yet. Create a team first, then add people to it."),
        options.length ? el("div", {}, el("button", { type: "button", onclick: addTeamRow }, "Add a team")) : null),
      err, el("div", {}, el("button", { type: "submit", class: "primary" }, "Create user")));
    if (options.length) addTeamRow();
    pw.input.value = generatePassword();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = form.elements;
      await busy(form.querySelector("button[type=submit]"), async () => {
        try {
          const made = await api("POST", "/api/admin/users", {
            name: f.name.value, email: f.email.value, temporary_password: pw.input.value, is_admin: f.is_admin.checked,
            memberships: teamRows.map((r) => ({ team_id: r.team.value, role: r.role.value })),
          });
          showCreated(made, pw.input.value);
        } catch (e) { showError(err, new Error(messageOf(e))); }
      });
    });
    openDialog("New user", form);
  }

  // After creating: show exactly what to hand to the new person.
  function showCreated(user, password) {
    const text = `Email: ${user.email}\nTemporary password: ${password}`;
    const copy = el("button", { type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(text); flash("Sign-in details copied.", true); } catch { flash("Copy failed. Select the text and copy it."); }
    } }, "Copy details");
    openDialog("User created", [
      el("p", {}, `${user.name} can now sign in. Pass these details on securely; they will be asked to choose their own password straight away.`),
      el("pre", { class: "credentials" }, text),
      el("div", { class: "row" }, copy,
        el("button", { type: "button", onclick: () => newUserDialog() }, "Add another user"),
        el("button", { type: "button", class: "primary", onclick: () => dlg.close() }, "Done"))]);
  }

  async function editUserDialog(userId) {
    const body = el("div", { class: "stack" });
    openDialog("Edit user", [body]);
    let options = [];
    try { options = await api("GET", "/api/admin/teams/options"); } catch { /* the form still works without it */ }

    async function render(user) {
      const err = dialogError();
      const run = async (fn, okMessage) => {
        try { const next = await fn(); showError(err, null); if (okMessage) flash(okMessage, true); if (next) await render(next); }
        catch (e) { showError(err, new Error(messageOf(e))); }
      };
      $("#admin-dialog-title").textContent = user.name;

      // ---- details ----
      const details = el("form", { class: "stack" },
        el("div", { class: "row" },
          el("label", {}, "Full name", el("input", { name: "name", value: user.name, maxlength: 100, required: true })),
          el("label", {}, "Email", el("input", { name: "email", type: "email", value: user.email, maxlength: 254, required: true }))),
        el("label", { class: "check" }, el("input", { name: "is_admin", type: "checkbox", checked: user.is_admin }), "Admin: can manage teams, users and membership"),
        el("label", { class: "check" }, el("input", { name: "active", type: "checkbox", checked: user.active }),
          "Active: can sign in (untick to deactivate; their history stays)"),
        el("div", {}, el("button", { type: "submit", class: "primary" }, "Save details")));
      details.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const f = details.elements;
        busy(details.querySelector("button"), () => run(() => api("PATCH", `/api/admin/users/${user.id}`, {
          name: f.name.value, email: f.email.value, is_admin: f.is_admin.checked, active: f.active.checked,
        }), "Details saved."));
      });

      // ---- teams ----
      const teamRows = user.teams.map((t) => {
        const role = el("select", { "aria-label": `Role in ${t.name}` }, el("option", { value: "member" }, "Member"), el("option", { value: "lead" }, "Lead"));
        role.value = t.role;
        role.addEventListener("change", () => run(async () => {
          try { return await api("PUT", `/api/admin/users/${user.id}/teams/${t.team_id}`, { role: role.value }); }
          catch (e) { role.value = t.role; throw e; }
        }));
        return el("li", { class: "member-row" }, el("span", { class: "who-block" }, el("strong", {}, t.name)), role,
          el("button", { type: "button", class: "danger", onclick: () => {
            if (!window.confirm(`Remove ${user.name} from ${t.name}? Their tasks in this team become unassigned.`)) return;
            run(() => api("DELETE", `/api/admin/users/${user.id}/teams/${t.team_id}`));
          } }, "Remove"));
      });
      const free = options.filter((o) => !user.teams.some((t) => t.team_id === o.id));
      const addTeam = el("select", { "aria-label": "Team to add" }, free.map((o) => el("option", { value: o.id }, o.name)));
      const addRole = el("select", { "aria-label": "Role" }, el("option", { value: "member" }, "Member"), el("option", { value: "lead" }, "Lead"));
      const addBtn = el("button", { type: "button", class: "primary", disabled: free.length === 0 }, "Add to team");
      addBtn.addEventListener("click", () => busy(addBtn, () => run(() =>
        api("PUT", `/api/admin/users/${user.id}/teams/${addTeam.value}`, { role: addRole.value }))));

      // ---- password ----
      const pw = passwordField("Set a new temporary password");
      const resetForm = el("form", { class: "stack" }, pw.node,
        el("div", {}, el("button", { type: "submit" }, "Reset password")));
      resetForm.addEventListener("submit", (ev) => {
        ev.preventDefault();
        busy(resetForm.querySelector("button[type=submit]"), () => run(async () => {
          await api("POST", `/api/admin/users/${user.id}/password`, { temporary_password: pw.input.value });
          pw.input.value = "";
          return api("GET", `/api/admin/users/${user.id}`);
        }, "Temporary password set. Pass it on; they must choose their own at next sign-in."));
      });

      body.replaceChildren(
        el("h3", {}, "Details"), details,
        el("h3", {}, "Teams"),
        user.teams.length ? el("ul", { class: "member-list-admin" }, teamRows) : el("p", { class: "muted" }, "Not in any team."),
        el("div", { class: "row add-row" }, addTeam, addRole, addBtn),
        el("p", { class: "hint" }, "A team with members always needs at least one lead."),
        el("h3", {}, "Password"), resetForm, err);
    }

    try { await render(await api("GET", `/api/admin/users/${userId}`)); } catch (e) { showError(dialogErrorFor(body), e); }
  }
  // Fallback error line if the user couldn't even be loaded.
  function dialogErrorFor(body) {
    const p = dialogError();
    body.replaceChildren(p);
    return p;
  }

  setRefresh(() => { loadSummary(); loadTeams(true); loadUsers(true); });
  await Promise.all([loadSummary(), loadTeams(true), loadUsers(true)]);
}
