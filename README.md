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
.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open <http://localhost:8000>. Interactive API docs are at <http://localhost:8000/docs>.

### Demo accounts (password `password` for every account)

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
.venv\Scripts\python scripts\check.py        # 41 behaviour checks against the database
node tests/test_edits.mjs                    # edit/conflict merge rules (frontend, no deps)
node tests/test_redirect.mjs                 # post-login redirect stays on this site
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

| | Not in team | Member | Lead |
|---|---|---|---|
| See the item | ✗ (looks like 404) | ✓ | ✓ |
| Comment | | ✓ | ✓ |
| Edit title / description / priority / due date | | ✓ (not when final) | ✓ (not when final) |
| Claim an unassigned item | | ✓ | ✓ |
| Start / block / unblock / complete / reopen | | only if assigned to them | ✓ any item |
| Assign to someone else, cancel | | | ✓ |
| Approve / reject | | | ✓, **but never their own item** |

The server enforces all of this on every request. The UI only shows the
buttons the server lists in `allowed_actions`.

### What "needs attention" means

The overview tiles and the `view=` list filter use the same SQL:

* **Assigned to me** — mine and not final
* **Unassigned urgent / high** — nobody owns it yet
* **Awaiting my approval** — in teams I lead, not created by me
* **Overdue** — past the due date and not final
* **No activity for 3+ days** — not final, nothing has happened in 72 hours

## API summary

| Method | Path | Notes |
|---|---|---|
| POST | `/api/login`, `/api/logout` | session cookie |
| GET | `/api/me` | user + teams with roles |
| GET | `/api/teams/{id}/members` | assign picker |
| GET | `/api/overview` | attention counts |
| GET | `/api/items` | `view, team_id, status, priority, type, assignee(me\|unassigned\|id), q, sort(priority\|updated\|created), page, page_size≤100` |
| POST | `/api/items` | create · **Idempotency-Key required** |
| GET | `/api/items/{id}` | includes `version` and `allowed_actions` |
| PATCH | `/api/items/{id}` | `expected_version` required → 409 `STALE_VERSION` if outdated |
| POST | `/api/items/{id}/claim` | 409 `ALREADY_CLAIMED` names the owner · Idempotency-Key |
| POST | `/api/items/{id}/assign` | lead only · `expected_version` · Idempotency-Key |
| POST | `/api/items/{id}/transitions` | `{action, reason?, expected_version}` · Idempotency-Key |
| POST | `/api/items/{id}/comments` | Idempotency-Key |
| GET | `/api/items/{id}/activity` | newest first, 50 per page, `before_id` cursor |

Errors always look like `{"error": {"code", "message", "details"}}`.

## Known limitations

* **No live push.** The item page polls every 10 s (and on tab focus); stale
  writes are refused, never applied, but you may see a change up to 10 s late.
* **No notifications or background jobs.** The attention views are how people
  find what needs them; there is no email/Slack delivery.
* **No admin screens.** Teams, users and memberships come from the seed script
  (or SQL). No sign-up, password reset or SSO; login is email + password.
* **CSRF** protection relies on `SameSite=Lax` session cookies and JSON request
  bodies; there are no CSRF tokens.
* **Page-number pagination.** If items change between page loads, a row can be
  skipped or shown twice. (The activity timeline uses a cursor and is stable.)
* **Idempotency keys are kept forever**; a cleanup job would delete old ones.
* **No attachments**, no editing of item type or team after creation.
* **Tests are a script, not a test framework**, and the frontend is checked by
  hand (two-browser checklist above), not by automated UI tests.
* Single app process + single database; no caching layer.
