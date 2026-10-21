# Newtonite Ops — operational work coordination

An internal web app for teams to create, own, progress, approve and discuss
operational work items (customer issues, incidents, payments, compliance
requests, tasks). It replaces "a request lost in a chat thread" with one place
that shows **what the work is, who owns it, what state it's in, what happened,
and what needs attention next**.

Design decisions and trade-offs are in [ENGINEERING_DECISIONS.md](ENGINEERING_DECISIONS.md).

## Stack

Python 3.11 · FastAPI · PostgreSQL (local install, managed with pgAdmin) ·
psycopg 3 with plain SQL · plain HTML/CSS/JavaScript (no build step).

## Setup

1. **PostgreSQL.** Install PostgreSQL (tested with 18) and create an empty
   database called `newtonite`, e.g. in pgAdmin: *Databases → Create → Database…*.
2. **Configuration.** Copy `.env.example` to `.env` and fill in your database
   user and password (`DB_USER`, `DB_PASSWORD`; or set `DATABASE_URL`) and a
   long random `SECRET_KEY`.
3. **Python environment** (from the project folder):

   ```bash
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt      # Windows
   # source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux
   ```

4. **Schema and demo data.** ⚠️ Both commands drop and recreate the app's tables.

   ```bash
   .venv\Scripts\python -m app.seed --items 20000    # schema + 6 teams, 40 users, 20k items
   # or: .venv\Scripts\python -m app.init_db         # empty schema only
   ```

## Run

```bash
.venv\Scripts\python -m app.migrate     # once, after pulling changes: keeps your data (see below)
.venv\Scripts\python -m uvicorn app.main:app --reload --timeout-graceful-shutdown 3
```

Open <http://localhost:8000>. Interactive API docs are at <http://localhost:8000/docs>.

### Upgrading an existing database

`python -m app.migrate` is safe to run any number of times. The first time after
this change it converts the six old fixed statuses into per-team statuses:
open → To do, in progress → In progress, done → Completed, awaiting approval →
To do with approval pending, and **Blocked** / **Cancelled** become custom statuses
(Cancelled counts as finished) in teams that had such tasks. Take a backup first
if the data matters (`pg_dump`).

### Projects

Admins create projects (Admin → Projects) and choose which teams work on each one.
A task can be put in one project of its own team, from the new-task form or the
task's Edit form, and the list and board can be filtered by project. Taking a team
off a project, or deleting the project, never deletes tasks: they leave the
project and the open ones move to the team's new default **Unassigned** status.
`python -m app.migrate` adds the tables and gives every existing team that status.

### Task types

Task types belong to each team. A team starts with Customer issue, Engineering,
Payment, Incident, Compliance and Task, and its leads and admins can rename, add and
remove any of them from the New task form: in the Type field, type a new name and press Enter to
add it, or use **Rename** / **Remove** under the field for the chosen type. The last type can't be removed, and removing one that tasks use asks which
type those tasks should get. The first `python -m app.migrate` after this change
converts the old fixed types; every task keeps its type.

Moving a task into the **Unassigned** status asks for confirmation and removes the
task's assignee. Moving a task out of Unassigned asks who should work on it.

### Accounts and admins

There is **no public sign-up**. Everyone's account is created by an **admin**.

* **First run (empty database):** opening the app shows a one-time
  **Create the first admin** page. That admin then creates teams and everyone else.
  The page closes itself once an account exists.
* **Existing database with users** (the setup page only appears on an empty one):
  make one of your users an admin from the command line.

  ```bash
  .venv\Scripts\python -m app.create_admin                      # list users and who is an admin
  .venv\Scripts\python -m app.create_admin --email you@mail.com # make that user an admin
  .venv\Scripts\python -m app.create_admin --email new@mail.com --name "New Person"   # create an admin (asks for a password)
  ```

  This is also the way back in if the last admin is ever locked out.
* **Adding people:** admins open **Admin** in the header, then **Users → New user**:
  name, email, a **temporary password** (there's a Generate button) and the teams
  the person joins, each as member or lead. The dialog then shows the sign-in
  details to pass on. **The new person must choose their own password the first
  time they sign in**, and can't use the app until they do. Admins can reset a
  forgotten password the same way.

### The admin dashboard

**Admin** appears in the header for admins only; the server refuses every
`/api/admin/*` request from anyone else (and `/admin.html` shows "Only admins can
open this page").

| Tab | What an admin can do |
|---|---|
| **Teams** | Create teams (each gets the three default statuses), rename them, delete them (type the team's name to confirm; this deletes its tasks too), and open **Members** to add people, change roles or remove them. |
| **Users** | Search users, create them, edit name and email, make someone an admin or take it away, **deactivate** or reactivate an account, reset a password, and add the user to teams or remove them. |

Rules the server enforces:

* A team that has members always has **at least one lead**: you can't demote or
  remove the last lead, and the first person added to an empty team must be a lead.
* The **last active admin** can't be demoted or deactivated, and nobody can
  deactivate themselves.
* A **deactivated** user is locked out immediately (their open sessions stop
  working), keeps their history, and can't be assigned work.
* Removing someone from a team **unassigns their tasks** in it, each with a
  history entry.
* Being an admin doesn't give access to tasks: an admin works on tasks only in
  teams they are a member of (they can add themselves). An admin in no team lands
  on the dashboard after signing in.

### Demo accounts (only after running the seed; password `password` for every account)

| Email | Teams and roles | Good for trying |
|---|---|---|
| `admin@newtonite.test` | no team, **admin** | the admin dashboard: teams, users, membership |
| `lead@newtonite.test` | Payments **lead**, Platform **lead** | approving, assigning, managing statuses |
| `lead2@newtonite.test` | Payments **lead** | approving items `lead@` created |
| `member@newtonite.test` | Payments member, Platform member | claiming, working items |
| `outsider@newtonite.test` | Support member | seeing that other teams' items are invisible |

Try two browsers (e.g. a normal and a private window) as `member@` and `lead2@`
on the same Payments item: claim it at the same moment, or edit it at the same time.

## Automated checks

```bash
.venv\Scripts\python -m unittest -v          # workflow + permission rules (no database)
.venv\Scripts\python scripts\check.py        # 74 behaviour checks against the database
node tests/test_edits.mjs                    # edit/conflict merge rules (frontend, no deps)
node tests/test_redirect.mjs                 # post-login redirect stays on this site
node tests/test_state.mjs                    # an older server response never replaces newer state
```

`scripts/check.py` runs against the configured database but creates its own
uniquely named team and users for each run and deletes them afterwards (also on
failure), so it is safe to run next to the demo data. It covers the risky
behaviour: concurrent claims (10 racing threads → exactly one winner),
stale edits, concurrent duplicate requests with one idempotency key, workflow
rules, team-scoped access, pagination, search with hostile input, and the
attention views. Add `-v` for tracebacks.

## How it works

```
 Browser (static/*.html + app.js)
    │  JSON over HTTP, session cookie, Idempotency-Key on every POST
    ▼
 FastAPI (app/)
    routes.py / auth.py      thin endpoints: validate → call service
    items.py                 work item operations (plain SQL)
    rules.py                 workflow + permissions (pure functions)
    idempotency.py           safe retries
    listing.py               lists, search, attention views
    │  one connection + one transaction per request
    ▼
 PostgreSQL
    work_items (with version + full-text search column)
    activity (append-only history)  idempotency_keys
    users  teams  memberships(role per team)
```

Every request runs in **one database transaction**: the change, its activity
row and its idempotency record commit together or not at all.

### Statuses

Every team has its own board columns (statuses):

* Three **defaults** that can't be renamed or deleted: **To do**, **In progress**
  and **Completed** (Completed counts as finished).
* Any number of **custom** statuses. Team **leads** add, rename, recolour,
  reorder and delete them with **Manage statuses** on the board. Members can read them.
* A status can be marked **counts as finished**. Tasks in a finished status can't
  be edited, claimed or reassigned, and drop out of *Assigned to me*, *Overdue*
  and *No activity*.
* Deleting a custom status that still has tasks asks where they should go; each
  moved task gets a history entry.
* A new task starts in **To do**. If you move a task to another team it keeps its
  place: same default status, or the custom status with the same name, otherwise To do.

**Moving a task:** the assignee, or any lead of the team, can move it to *any*
status of its team, in any order, by drag and drop or the status dropdown. There
are no required reasons.

### Approval

Ticking *Needs a lead's approval* when creating a task adds an approval step:

* Until a lead approves it, **nobody can change its status** (it stays in To do).
* A lead approves or rejects it from the task page. **The task's creator can never
  approve or reject their own task**, even if they're a lead.
* A **rejected** task is closed and locked, but another lead can still approve it later.
* *Awaiting my approval* on the List page lists the pending tasks in teams you lead.

### Who can do what

Roles are **per team** (`member` or `lead`); one person can be a lead in one
team and a member in another.

| | Not in team | Requester (created it from another team) | Member | Lead |
|---|---|---|---|---|
| See the item | ✗ (looks like 404) | ✓ | ✓ | ✓ |
| Comment | | ✓ | ✓ | ✓ |
| Edit details, move to another team | | ✓ | ✓ unless **restricted** | ✓ |
| Tick / untick "Restrict editing" | | ✓ (they're the creator) | only the creator | ✗ (leads can always edit, so they don't get the option) |
| Claim an unassigned item | | | ✓ | ✓ |
| Change status (any status of the team) | | | only if assigned to them | ✓ any task |
| Assign when creating | | | ✓ anyone in the team | ✓ anyone in the team |
| Reassign later | | | only if **they** made the current assignment | ✓ |
| Manage the team's statuses | | | | ✓ |
| Approve / reject | | | | ✓, **but never their own item** |

* **Restrict editing** (a checkbox on create and edit, shown to a creator who isn't
  a lead): only the team's leads and the creator can change the title,
  description, priority, due date or team.
  Status changes are *not* locked, so the assignee can still move the work along.
* **Cross-team requests:** you can create a task in any team. If you're not in
  it, the task is your request: that team does the work, and you follow it under
  *Requested by me*, comment on it and edit it.
* **Assigning:** whoever assigns a task (and any lead) can reassign it later.
  The person it's assigned to can't pass it on to someone else. The task page
  shows who assigned it. If a lead reassigns it, that lead is now the assigner.
* **Moving a task** to another team is part of editing. If the assignee isn't in
  the new team, the task becomes unassigned. If you're not in the new team and
  didn't create the task, it leaves your list.

The server enforces all of this on every request. The UI only shows the
buttons the server lists in `allowed_actions`. Admins (see above) manage teams,
users and membership, but have no extra rights over tasks.

### What "needs attention" means

The overview tiles and the `view=` list filter use the same SQL:

* **Assigned to me** — mine and not final
* **Unassigned urgent / high** — nobody owns it yet
* **Awaiting my approval** — in teams I lead, not created by me
* **Overdue** — past the due date and not final
* **No activity for 3+ days** — not final, nothing has happened in 72 hours
* **Requested by me** — tasks I created in teams I'm not part of

### List and board

The header has two views of the same tasks: **List** (attention tiles, filters,
and a status dropdown on every row) and **Board** (one column per status).

* **Drag and drop:** drag a card to another column to change its status. Every
  card also has a status dropdown, for keyboard and touch use.
* **One team at a time:** each team has its own statuses, so the board has a team
  picker. Leads of the team also see **Manage statuses**.
* **Fixed width, scrolls sideways:** columns have a fixed width, so a board with
  many statuses scrolls sideways, and cards keep their width when other columns fold.
* **Folding:** a folded column shows only its name and count, and you can still
  drop cards on it. Finished statuses start folded; click a column to open it or
  use its ‹ button to fold it. The browser remembers your choice.
* **Quick filters:** *My tasks*, *Unassigned*, *Urgent & high*, *Overdue*, plus search.
  Columns load 25 cards at a time, and their counts come from the database.

### Profile

Click your name in the header. The profile page shows your account (name, email),
a summary of your work (assigned to you, overdue, finished by you, created by you),
and each team you're in with your role, its members and its task counts. You can
change your display name and your password (the current password is required).

### Live updates

Pages never refresh on their own. When someone else changes a task you can see (or
a lead changes your team's statuses), the server pushes a small notice
(Server-Sent Events). The page then shows *"3 tasks have changed since you loaded
this board" · Refresh* (one task changed several times counts once) and reloads
only when you click. The **Refresh** button in the header works at any time. After
the connection drops and comes back, the page tells you that you may have missed
changes. Saving over someone else's newer change is still refused (`STALE_VERSION`).

## API summary

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/api/setup` | first-run only: is setup needed? / create the first admin (409 once any user exists) |
| GET | `/api/teams` | team names, for choosing where to send a task (login required) |
| POST | `/api/login`, `/api/logout` | session cookie |
| GET | `/api/me` | user (incl. `is_admin`, `must_change_password`) + teams with roles and statuses |
| GET | `/api/teams/{id}/members` | assign picker |
| GET | `/api/overview` | attention counts |
| GET | `/api/items` | `view, team_id, status (name), status_id, priority, type, assignee(me\|unassigned\|id), q, sort(priority\|updated\|created), page, page_size≤100` |
| POST | `/api/items` | create (any team; optional `assignee_id`, `restricted`) · **Idempotency-Key required** |
| GET | `/api/items/{id}` | includes `version` and `allowed_actions` |
| PATCH | `/api/items/{id}` | details, `restricted`, `team_id` (move), `assignee_id` · `expected_version` required → 409 `STALE_VERSION` if outdated |
| POST | `/api/items/{id}/claim` | 409 `ALREADY_CLAIMED` names the owner · Idempotency-Key |
| POST | `/api/items/{id}/assign` | lead only · `expected_version` · Idempotency-Key |
| POST | `/api/items/{id}/status` | `{status_id, expected_version}` · assignee or lead · Idempotency-Key |
| POST | `/api/items/{id}/approval` | `{decision: approve\|reject, reason?, expected_version}` · a lead who isn't the creator · Idempotency-Key |
| GET | `/api/teams/{id}/statuses` | the team's statuses, in board order |
| POST | `/api/teams/{id}/statuses` | `{name, color, finished}` · leads only |
| PATCH | `/api/teams/{id}/statuses/{sid}` | rename, recolour, `finished` · leads only (defaults: colour only) |
| PUT | `/api/teams/{id}/statuses/order` | `{ids: [...]}` every status once · leads only |
| DELETE | `/api/teams/{id}/statuses/{sid}?move_to=` | custom only; `move_to` required if it has tasks (409 `STATUS_IN_USE`) |
| GET / PATCH | `/api/profile` | account, teams with members, work summary / change display name |
| POST | `/api/profile/password` | `{current_password, new_password}`; the only action allowed while holding a temporary password |
| GET | `/api/admin/summary` | **admin only** (all `/api/admin/*` are): counts of teams, users, admins |
| GET / POST | `/api/admin/teams` | list (`q`, `page`) / create `{name}` |
| GET | `/api/admin/teams/options` | every team's id and name (for pickers) |
| PATCH / DELETE | `/api/admin/teams/{id}` | rename `{name}` / delete `{confirm_name}` |
| GET | `/api/admin/teams/{id}/members` | members with role and active flag |
| GET / POST | `/api/admin/users` | list (`q`, `page`) / create `{name, email, temporary_password, is_admin?, memberships: [{team_id, role}]}` |
| GET / PATCH | `/api/admin/users/{id}` | read / change `name`, `email`, `is_admin`, `active` (409 `LAST_ADMIN`, `SELF_DEACTIVATE`) |
| POST | `/api/admin/users/{id}/password` | `{temporary_password}`: the user must change it at next sign-in |
| PUT / DELETE | `/api/admin/users/{id}/teams/{team_id}` | add or change role `{role}` / remove (409 `TEAM_NEEDS_LEAD`) |
| POST | `/api/items/{id}/comments` | Idempotency-Key |
| GET | `/api/items/{id}/activity` | newest first, 50 per page, `before_id` cursor |
| GET | `/api/items/counts` | tasks per status id for one team (`team_id` required) with the same filters as `/api/items` |
| GET | `/api/events` | live-update stream (`text/event-stream`): `{type: "change", item_id, team_ids, actor_id, kind}` |

Errors always look like `{"error": {"code", "message", "details"}}`.

## Known limitations

* **Live updates say *that* something changed, not *what*.** You click Refresh
  to see it. A notice is only sent once the change has been saved for good.
  Notices about tasks moved out of your team stop when you reload the page.
* **Uvicorn waits for open live-update streams on shutdown**, which is why the
  run command includes `--timeout-graceful-shutdown 3`.
* **Dragging cards needs a mouse.** On touch screens use the card's status dropdown.
  A lead's rejection reason is asked for with the browser's prompt box.
* **Statuses are per team.** Renaming a custom status doesn't rename it in other
  teams, and the List page's status filter matches by name across your teams.
* **Fonts load from Google Fonts.** Offline, the app falls back to system fonts.
* **No notifications or background jobs.** The attention views are how people
  find what needs them; there is no email/Slack delivery.
* **No self-service password reset or SSO.** Login is email + password; a forgotten
  password is reset by an admin, who then passes on a temporary one. If an admin
  forgets theirs, another admin resets it (or use `python -m app.create_admin`).
* **Temporary passwords are shown to the admin and passed on by hand**; there is no
  email delivery. Users can't be deleted (only deactivated, so history stays).
* **No audit log of admin actions** (who added or removed whom). Task history does
  record when a removed member's tasks were unassigned, and by which admin.
* **CSRF** protection relies on `SameSite=Lax` session cookies and JSON request
  bodies; there are no CSRF tokens.
* **Page-number pagination.** If items change between page loads, a row can be
  skipped or shown twice. (The activity timeline uses a cursor and is stable.)
* **Idempotency keys are kept forever**; a cleanup job would delete old ones.
* **No attachments**, no editing of item type or team after creation.
* **Tests are a script, not a test framework**, and the frontend is checked by
  hand (two-browser checklist above), not by automated UI tests.
* Single app process + single database; no caching layer.
