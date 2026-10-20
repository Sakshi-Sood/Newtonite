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
.venv\Scripts\python -m app.migrate     # once, after pulling changes: adds new columns, keeps your data
.venv\Scripts\python -m uvicorn app.main:app --reload --timeout-graceful-shutdown 3
```

Open <http://localhost:8000>. Interactive API docs are at <http://localhost:8000/docs>.

### Creating accounts

On the sign-in page choose **Create an account**. Enter your name, email and a
password (8+ characters), then either:

* **create a new team** — you become its **lead** (the first person in must do this), or
* **join an existing team** — you become a **member**.

You are signed in straight away. With an empty database, sign up once to create
a team, then sign up a second person who joins it to try claiming, approving and
editing the same item from two browsers.


### Demo accounts (only after running the seed; password `password` for every account)

| Email | Teams and roles | Good for trying |
|---|---|---|
| `lead@newtonite.test` | Payments **lead**, Platform **lead** | approving, assigning, cancelling |
| `lead2@newtonite.test` | Payments **lead** | approving items `lead@` created |
| `member@newtonite.test` | Payments member, Platform member | claiming, working items |
| `outsider@newtonite.test` | Support member | seeing that other teams' items are invisible |

Try two browsers (e.g. a normal and a private window) as `member@` and `lead2@`
on the same Payments item: claim it at the same moment, or edit it at the same time.

## Automated checks

```bash
.venv\Scripts\python -m unittest -v          # workflow + permission rules (no database)
.venv\Scripts\python scripts\check.py        # 59 behaviour checks against the database
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

### Workflow

```
awaiting_approval ──approve──▶ open ──start──▶ in_progress ──complete──▶ done
        │                                       │      ▲                   │
      reject                                  block  unblock            reopen
        ▼                                       ▼      │                   │
    cancelled                                  blocked ┘    (back to in_progress)

cancel: open | in_progress | blocked ──▶ cancelled
```

| Action | From | To | Needs |
|---|---|---|---|
| approve / reject | awaiting_approval | open / cancelled | a lead who didn't create it; reject needs a reason |
| start | open | in_progress | an assignee |
| block / unblock | in_progress ↔ blocked | | block needs a reason |
| complete | in_progress | done | a reason (resolution note) |
| reopen | done | in_progress | |
| cancel | open, in_progress, blocked | cancelled | a lead, and a reason |

* Items flagged "needs approval" start in `awaiting_approval`; others start `open`.
* `start` needs an assignee. `reject`, `block`, `complete` and `cancel` need a
  reason, which is kept in the history ("why was this decided?").
* `done` and `cancelled` are final (a `done` item can be reopened).

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
| Start / block / unblock / complete / reopen | | | only if assigned to them | ✓ any item |
| Assign when creating | | | ✓ anyone in the team | ✓ anyone in the team |
| Reassign later | | | only if **they** made the current assignment | ✓ |
| Cancel | | | | ✓ |
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
buttons the server lists in `allowed_actions`.

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
and a status dropdown on every row) and **Board** (one column per status). On
the board, drag a card to another column to change its status. While you drag,
columns the card can't move to fade out, and moves that need a reason (block,
complete, cancel, reject) ask for one. Every card also has the same status
dropdown, for keyboard and touch use. Quick filter chips: *My tasks*,
*Unassigned*, *Urgent & high*, *Overdue*, *Requested by me*, plus team and
search. Columns load 25 cards at a time, and their counts come from the database.

### Live updates

Pages never refresh on their own. When someone else changes a task you can see,
the server pushes a small notice (Server-Sent Events). The page then shows
*"3 tasks have changed since you loaded this board" · Refresh* (one task changed
several times counts once) and reloads only when you click. The **Refresh** button in the header works at any time. The dot
next to it shows whether the live connection is up. After the connection drops
and comes back, the page tells you that you may have missed changes. Saving
over someone else's newer change is still refused, as before (`STALE_VERSION`).

## API summary

| Method | Path | Notes |
|---|---|---|
| POST | `/api/signup` | name, email, password + `team_id` (join as member) **or** `new_team_name` (create, as lead) |
| GET | `/api/teams` | team names for the sign-up form (no login needed) |
| POST | `/api/login`, `/api/logout` | session cookie |
| GET | `/api/me` | user + teams with roles |
| GET | `/api/teams/{id}/members` | assign picker |
| GET | `/api/overview` | attention counts |
| GET | `/api/items` | `view, team_id, status, priority, type, assignee(me\|unassigned\|id), q, sort(priority\|updated\|created), page, page_size≤100` |
| POST | `/api/items` | create (any team; optional `assignee_id`, `restricted`) · **Idempotency-Key required** |
| GET | `/api/items/{id}` | includes `version` and `allowed_actions` |
| PATCH | `/api/items/{id}` | details, `restricted`, `team_id` (move), `assignee_id` · `expected_version` required → 409 `STALE_VERSION` if outdated |
| POST | `/api/items/{id}/claim` | 409 `ALREADY_CLAIMED` names the owner · Idempotency-Key |
| POST | `/api/items/{id}/assign` | lead only · `expected_version` · Idempotency-Key |
| POST | `/api/items/{id}/transitions` | `{action, reason?, expected_version}` · Idempotency-Key |
| POST | `/api/items/{id}/comments` | Idempotency-Key |
| GET | `/api/items/{id}/activity` | newest first, 50 per page, `before_id` cursor |
| GET | `/api/items/counts` | tasks per status for the same filters as `/api/items` (board column counts) |
| GET | `/api/events` | live-update stream (`text/event-stream`): `{type: "change", item_id, team_ids, actor_id, kind}` |

Errors always look like `{"error": {"code", "message", "details"}}`.

## Known limitations

* **Live updates say *that* something changed, not *what*.** You click Refresh
  to see it. A notice is only sent once the change has been saved for good.
  Notices about tasks moved out of your team stop when you reload the page.
* **Uvicorn waits for open live-update streams on shutdown**, which is why the
  run command includes `--timeout-graceful-shutdown 3`.
* **Reasons are asked for with the browser's prompt box**, and dragging cards
  needs a mouse (on touch screens, use the card's status dropdown).
* **Fonts load from Google Fonts.** Offline, the app falls back to system fonts.
* **No notifications or background jobs.** The attention views are how people
  find what needs them; there is no email/Slack delivery.
* **Open sign-up.** Anyone who can reach the app can create an account and join
  any team as a member (team names are listed on the sign-up page). There is no
  email verification, invite or approval step, and no screen for adding a second
  team or promoting someone to lead afterwards (SQL only).
* No password reset or SSO; login is email + password.
* **CSRF** protection relies on `SameSite=Lax` session cookies and JSON request
  bodies; there are no CSRF tokens.
* **Page-number pagination.** If items change between page loads, a row can be
  skipped or shown twice. (The activity timeline uses a cursor and is stable.)
* **Idempotency keys are kept forever**; a cleanup job would delete old ones.
* **No attachments**, no editing of item type or team after creation.
* **Tests are a script, not a test framework**, and the frontend is checked by
  hand (two-browser checklist above), not by automated UI tests.
* Single app process + single database; no caching layer.
