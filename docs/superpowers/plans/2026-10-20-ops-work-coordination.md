# Operations Work Coordination App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (execution method chosen: **native**). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An internal web app where teams create, own, progress, approve and discuss operational work items. The server stays correct when two people act at once, when someone acts on stale data, and when a request is repeated.

**Architecture:** One FastAPI application. It serves a JSON API under `/api` and a small plain HTML + JavaScript frontend from `static/`, with no build step. Data lives in the locally installed PostgreSQL and is accessed with plain SQL through psycopg 3. Each HTTP request runs in exactly one database transaction, which writes both the change and an append-only `activity` row. Workflow and permission rules live in one pure Python module.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, psycopg 3 (+ `psycopg_pool`), PostgreSQL 18 (local Windows service, managed with pgAdmin), Pydantic v2, Starlette `SessionMiddleware` (signed cookie). Frontend: vanilla HTML/CSS/JS.

**Spec:** `Newtonite SoftwareEngineering Challenge.pdf` (repo root). The brief leaves product decisions open on purpose; this plan makes them (see *Product Decisions*).

## Global Constraints

- **Keep it simple.** Don't add:
  - Docker
  - an ORM or a migration framework
  - a frontend build step
  - background workers or queues
  - pytest or a separate test database

  Use one schema file and one app process.
- Python 3.11 in a virtualenv at `.venv`. `requirements.txt` lists only: `fastapi`, `uvicorn[standard]`, `psycopg[binary]`, `psycopg_pool`, `pydantic`, `itsdangerous`, `python-dotenv`.
- PostgreSQL is the local install (service `postgresql-x64-18`, port 5432) with a single database, `newtonite`, created in pgAdmin. Configuration lives in `.env` (`DATABASE_URL`, `SECRET_KEY`); `.env.example` is committed and `.env` is git-ignored.
- **Automated checks** (the brief requires "automated tests for important behaviour"):
  - `python -m unittest` runs `tests/test_rules.py`. These are pure-logic tests and need no database.
  - `python scripts/check.py` runs the DB-backed behaviour checks against `newtonite`. Every run creates its own uniquely named team and users and deletes them at the end, even if a check fails. It prints `PASS`/`FAIL` per check and exits 1 if any check failed. **Each task below adds its checks to this script.**
- "Your solution should not depend on loading the entire dataset into the browser or processing everything in application memory":
  - Filtering, search and sorting happen in SQL.
  - Item lists are paged, 25 per page by default and at most 100.
  - The activity timeline loads 50 entries per page.
- Every change to a work item happens inside the request's transaction together with an `activity` row ("Important actions should not silently disappear").
- Authorization is checked on the server for every request. The UI only shows the `allowed_actions` that the server returns.
- Error body everywhere: `{"error": {"code": str, "message": str, "details": dict | null}}`.
- Deliverables at the repo root: `README.md` (setup, run, checks, known limitations) and `ENGINEERING_DECISIONS.md` (about 5 decisions with trade-offs).
- Work on branch `feat/ops-app`. Append `.venv/`, `__pycache__/` and `.env` to the existing `.gitignore`, keeping its current lines.

## Product Decisions (made by this plan)

- **Work item fields:**
  - `title`, `description`
  - `type`: `customer_issue | engineering | payment | incident | compliance | task`
  - `priority`: `urgent | high | medium | low`
  - `status`, `assignee` (nullable), `requires_approval`, `due_at` (nullable)
  - `team`, `created_by`, `version`, `last_activity_at`
- **Statuses:** `awaiting_approval`, `open`, `in_progress`, `blocked`, `done`, `cancelled`.
  - `done` and `cancelled` are terminal.
  - Items with `requires_approval` start in `awaiting_approval`; all others start in `open`.
- **Transitions** (`action: from → to`):
  - `approve: awaiting_approval → open`
  - `reject: awaiting_approval → cancelled`
  - `start: open → in_progress` (needs an assignee)
  - `block: in_progress → blocked`
  - `unblock: blocked → in_progress`
  - `complete: in_progress → done`
  - `reopen: done → in_progress`
  - `cancel: open | in_progress | blocked → cancelled`

  A **reason is required** for `reject`, `block`, `complete` and `cancel`. It's stored in the activity entry, which records why each decision was made.
- **Roles:** every team membership is either `member` or `lead`, and a user can be in several teams with different roles. There is no global admin.
  - **Not in the item's team:** the API answers 404, as if the item didn't exist.
  - **member:**
    - can view and comment
    - can edit while the item isn't terminal
    - can claim it while it's unassigned and not terminal
    - can `start`, `block`, `unblock`, `complete` and `reopen` only items assigned to them
  - **lead:** everything a member can do on any item in the team, plus `assign`, `cancel`, `approve` and `reject`.
  - **The creator of an item can never `approve` or `reject` it.**
- **Attention views** (used by the overview counts and by the list's `view=` filter):
  - `mine`: assigned to me, not terminal
  - `unassigned_urgent`: unassigned, urgent or high priority, not terminal
  - `awaiting_my_approval`: in `awaiting_approval`, in a team where I'm lead, not created by me
  - `overdue`: `due_at < now`, not terminal
  - `stale`: not terminal, `last_activity_at < now − 72h`
- **Freshness:** the item page polls every 10 seconds. Every edit carries `expected_version`, so a write based on stale data is refused with 409 instead of overwriting someone else's change.
- **Comments** are activity entries (`kind='comment'`), so each item has a single timeline. A comment updates `last_activity_at` but not `version`.
- **Deliberately not built:**
  - notifications and background jobs (the attention views answer "what needs me")
  - live push
  - attachments
  - admin screens for teams and users (these come from the seed script)

## Review Focus

These are inputs no task's normal path exercises, with the behaviour a user would expect from each. Each one has a check in `scripts/check.py`, added by the task named.

1. **Membership removed while a user is still logged in.** Their next request gets 404, because membership is read on every request and never cached in the cookie. *→ Task 4.*
2. **An item with a very long history.** The timeline returns 50 entries plus `next_before_id`, never everything. *→ Task 4.*
3. **Search text with operators or garbage** (`foo & (bar`, `"`, only spaces). The search returns results, never a 500. *→ Task 8.*
4. **Assigning to someone outside the item's team.** The API answers 422 `INVALID_ASSIGNEE` and the item is unchanged. *→ Task 7.*
5. **A malformed item id** (`abc`). The API answers 404 `NOT_FOUND`, not a Postgres error. *→ Task 4.*

---

## File Structure

```
requirements.txt
.env.example
app/
  config.py         # loads .env: DATABASE_URL, SECRET_KEY
  db.py             # pool, get_conn dependency (one transaction per request)
  schema.sql        # whole schema (drop + create)
  init_db.py        # python -m app.init_db -> applies schema.sql
  errors.py         # AppError + exception handlers
  rules.py          # statuses, transitions, permissions (pure, no DB)
  auth.py           # password hashing, login/logout/me, current_user dependency
  items.py          # item service functions (SQL); used by routes and scripts/check.py
  idempotency.py    # run_idempotent
  listing.py        # list/search/attention views/overview SQL
  routes.py         # /api item endpoints (thin: validate -> call service)
  main.py           # create_app(); `uvicorn app.main:app`
  seed.py           # python -m app.seed --items 20000
static/
  index.html  item.html  new.html  login.html  app.js  style.css
tests/
  test_rules.py     # unittest, no DB
scripts/
  check.py          # DB-backed behaviour checks (self-cleaning)
README.md
ENGINEERING_DECISIONS.md
```

**How `scripts/check.py` is organised.** It opens a pool on `DATABASE_URL` and creates a fixture team named `check-<uuid8>` plus users `<uuid8>-<name>@check.test` (password `password`). It runs every function whose name starts with `check_` and prints `PASS name` or `FAIL name: <reason>`. In a `finally` block it deletes the activity, items, memberships, users and team it created. Helpers:

- `team() -> dict`
- `user(role: str | None, team_id=None) -> dict` (with `role=None`, the user belongs to no team)
- `item(created_by, **overrides) -> dict`
- `expect_error(fn, status: int, code: str) -> AppError`, which fails the check unless `fn()` raises that error.

Concurrency checks run service functions in a `ThreadPoolExecutor`. Each thread uses its own pooled connection inside `with conn.transaction():`.

---

### Task 1: Project setup and schema

**Files:**
- Create: `requirements.txt`, `.env.example`, `app/__init__.py`, `app/config.py`, `app/db.py`, `app/schema.sql`, `app/init_db.py`, `scripts/check.py` (with the helper framework above)
- Modify: `.gitignore`

**Interfaces:**
- Produces:
  - `create_pool(url: str) -> ConnectionPool`, with `min_size=2, max_size=10, kwargs={"row_factory": dict_row}`.
  - `get_conn(request) -> Iterator[Connection]`, a FastAPI dependency. It yields inside `with request.app.state.pool.connection() as conn:`, so a request commits on success and rolls back on any exception.
  - `apply_schema(conn) -> None`.

`schema.sql` begins with `DROP … IF EXISTS … CASCADE` so it can be re-run. Contents:

- **Enum types:**
  - `item_status`
  - `item_priority`, declared as `'urgent','high','medium','low'` in that order so that `ORDER BY priority` puts urgent first
  - `item_type`
- **`users`**: `id uuid pk default gen_random_uuid()`, `email text unique not null`, `name text not null`, `password_hash text not null`.
- **`teams`**: `id uuid pk default gen_random_uuid()`, `name text unique not null`.
- **`memberships`**: `user_id uuid fk on delete cascade`, `team_id uuid fk on delete cascade`, `role text not null check (role in ('member','lead'))`, primary key `(user_id, team_id)`.
- **`work_items`**:
  - `id uuid pk default gen_random_uuid()`
  - `team_id fk not null`
  - `title text not null`, `description text not null default ''`
  - `type item_type not null`, `priority item_priority not null`, `status item_status not null`
  - `assignee_id uuid null fk`, `requires_approval bool not null`, `due_at timestamptz null`
  - `created_by uuid fk not null`
  - `created_at` and `updated_at` (`timestamptz not null default now()`), `last_activity_at timestamptz not null default now()`
  - `version int not null default 1`
  - `search tsvector generated always as (setweight(to_tsvector('english', title),'A') || setweight(to_tsvector('english', description),'B')) stored`
- **`activity`**: `id bigserial pk`, `work_item_id uuid fk not null on delete cascade`, `actor_id uuid fk not null`, `kind text not null`, `payload jsonb not null default '{}'`, `created_at timestamptz not null default now()`.
- **`idempotency_keys`**: `user_id uuid fk on delete cascade`, `key text`, `request_hash text not null`, `status_code int not null`, `response jsonb not null`, `created_at timestamptz default now()`, primary key `(user_id, key)`.
- **Indexes:**
  - GIN on `work_items(search)`
  - `work_items(team_id, status, priority, last_activity_at desc)`
  - `work_items(assignee_id, status)`
  - `activity(work_item_id, id desc)`
  - `memberships(team_id)`

- [ ] **Step 1:** Create the database `newtonite` in pgAdmin, then copy `.env.example` to `.env` and fill in the postgres password.
- [ ] **Step 2:** Write all the Task 1 files. `check.py` gets one check to start with: `check_schema`, which asserts that all six tables exist and that `enum_range(null::item_priority)` is `['urgent','high','medium','low']`.
- [ ] **Step 3:** Run `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `python -m app.init_db` and `python scripts/check.py`. Expected: `PASS check_schema`, exit code 0, and the tables visible in pgAdmin.
- [ ] **Step 4:** Commit with `git commit -m "chore: project setup and postgres schema"`.

---

### Task 2: Workflow and permission rules

**Files:**
- Create: `app/rules.py`, `tests/__init__.py`, `tests/test_rules.py`

**Interfaces:**
- Produces:
  - Constants:
    - `STATUSES`, `PRIORITIES`, `ITEM_TYPES`, `ROLES`
    - `TERMINAL = {"done", "cancelled"}`
    - `ACTIONS` (the 8 transitions)
    - `REASON_REQUIRED = {"reject", "block", "complete", "cancel"}`
  - `TRANSITIONS: dict[str, dict[str, str]]`, mapping status → action → new status, exactly as listed in *Product Decisions*.
  - `next_status(status: str, action: str) -> str | None`.
  - `allowed_actions(item: dict, user_id: str, role: str | None) -> list[str]`.
    - `item` must have the keys `status`, `assignee_id` and `created_by`.
    - The result is drawn from `view`, `edit`, `comment`, `claim`, `assign` and the transition actions.
    - A transition is included only when `next_status` is not None and the role rule allows it.
    - If `role` is None, the result is `[]`.
  - `can(item, user_id, role, permission: str) -> bool`.

- [ ] **Step 1:** Write `tests/test_rules.py` with `unittest.TestCase`.

```python
ITEM = {"status": "in_progress", "assignee_id": "u2", "created_by": "u3"}

class RulesTest(unittest.TestCase):
    def test_transitions(self):
        self.assertEqual(next_status("open", "start"), "in_progress")
        self.assertIsNone(next_status("done", "cancel"))
        self.assertIsNone(next_status("awaiting_approval", "start"))
    def test_non_member_gets_nothing(self):
        self.assertEqual(allowed_actions(ITEM, "u1", None), [])
    def test_member_only_completes_own_item(self):
        self.assertFalse(can(ITEM, "u1", "member", "complete"))
        self.assertTrue(can(ITEM, "u2", "member", "complete"))
    def test_lead_can_complete_any_and_assign(self):
        self.assertTrue(can(ITEM, "u1", "lead", "complete") and can(ITEM, "u1", "lead", "assign"))
    def test_member_cannot_assign_or_cancel(self):
        self.assertFalse(can(ITEM, "u2", "member", "assign") or can(ITEM, "u2", "member", "cancel"))
    def test_creator_cannot_approve_own_item(self):
        item = {"status": "awaiting_approval", "assignee_id": None, "created_by": "u1"}
        self.assertFalse(can(item, "u1", "lead", "approve"))
        self.assertTrue(can(item, "u9", "lead", "approve"))
    def test_terminal_allows_comment_only(self):
        acts = allowed_actions({"status": "cancelled", "assignee_id": None, "created_by": "u3"}, "u1", "lead")
        self.assertIn("comment", acts); self.assertNotIn("edit", acts); self.assertNotIn("claim", acts)
    def test_claim_only_when_unassigned(self):
        self.assertFalse(can(ITEM, "u1", "member", "claim"))
```

- [ ] **Step 2:** Run `python -m unittest -v`. Expected: it fails with an ImportError.
- [ ] **Step 3:** Implement `app/rules.py`.
- [ ] **Step 4:** Run `python -m unittest -v`. Expected: all 8 tests pass.
- [ ] **Step 5:** Commit with `feat: workflow transitions and permission rules`.

---

### Task 3: App, error envelope, login

**Files:**
- Create: `app/errors.py`, `app/auth.py`, `app/main.py`

**Interfaces:**
- Produces:
  - `class AppError(Exception)` with `__init__(self, status: int, code: str, message: str, details: dict | None = None)`.
  - Exception handlers:
    - `AppError` returns its own status with the error envelope.
    - `RequestValidationError` returns 400 `VALIDATION_FAILED` with `details={"errors": exc.errors()}`.
    - Anything unhandled returns 500 `INTERNAL`. It is logged, and the response body contains no traceback.
  - `hash_password(pw: str) -> str` and `verify_password(pw: str, stored: str) -> bool`. They use `hashlib.scrypt` with a random 16-byte salt and store the result as `"scrypt$<salt_hex>$<hash_hex>"`.
  - `current_user(request, conn) -> dict`, a dependency that reads `request.session["user_id"]` and loads `{id, email, name}`. If the session has no user, or the user no longer exists, it raises 401 `UNAUTHENTICATED`.
  - `create_app() -> FastAPI`:
    - adds `SessionMiddleware(secret_key=SECRET_KEY, max_age=12*3600, same_site="lax")`
    - creates the pool on startup and closes it on shutdown
    - includes the routers
    - mounts `static/` at `/` with `html=True`
    - `app.main:app = create_app()`
  - Routes:
    - `POST /api/login {email, password}` returns 200 `{"user": {...}}`. Wrong credentials return 401 `INVALID_CREDENTIALS` with the same message whether or not the email exists.
    - `POST /api/logout` returns 204.
    - `GET /api/me` returns `{"user": {...}, "teams": [{"id", "name", "role"}]}`.
    - `GET /api/teams/{team_id}/members` returns `[{"id", "name", "role"}]`, or 404 if the caller isn't in that team.

- [ ] **Step 1:** Implement the three files.
- [ ] **Step 2:** Add `check_password_hash_roundtrip` to `check.py`: `verify_password` returns True for the right password and False for a wrong one, and two hashes of the same password differ.
- [ ] **Step 3:** Verify:
  - Run `python scripts/check.py`. Expected: all checks pass.
  - Insert one user through `python -c` using `hash_password`, then run `uvicorn app.main:app --reload`.
  - In `http://localhost:8000/docs`:
    - `GET /api/me` returns 401 `UNAUTHENTICATED`.
    - Logging in with the wrong password returns 401 `INVALID_CREDENTIALS`.
    - Logging in with the right password returns 200.
    - After that, `/api/me` returns the user.
- [ ] **Step 4:** Commit with `feat: app shell, error envelope and session login`.

---

### Task 4: Create and read items, access checks, timeline

**Files:**
- Create: `app/items.py`, `app/routes.py`
- Modify: `app/main.py`, `scripts/check.py`

**Interfaces:**
- Consumes: `allowed_actions` and `can` from Task 2; `AppError` and `current_user` from Task 3.
- Produces, in `app/items.py` (each function takes the request's `conn`):
  - `role_in_team(conn, user_id, team_id) -> str | None`.
  - `load_item(conn, item_id: str, user_id: str, for_update: bool = False) -> tuple[dict, str]` returns `(row, role)`. It raises 404 `NOT_FOUND` when:
    - `item_id` is not a valid UUID (check with `uuid.UUID` before querying)
    - the item doesn't exist
    - the user's role in its team is None
  - `require(item, user_id, role, permission) -> None` raises 403 `FORBIDDEN` with `details={"permission": ...}`.
  - `record_activity(conn, item_id, actor_id, kind: str, payload: dict) -> int`.
    - Inserts an activity row and sets `last_activity_at = now()` on the item.
    - `kind` is one of `created | updated | comment | assigned | status_changed`.
  - `item_dto(conn, row, user_id, role) -> dict` returns:
    - `id`, `team: {id, name}`, `title`, `description`, `type`, `priority`, `status`, `requires_approval`, `due_at`
    - `assignee: {id, name} | None`, `created_by: {id, name}`
    - `created_at`, `updated_at`, `last_activity_at`, `version`, `allowed_actions`
  - `class ItemCreate(BaseModel)` has:
    - `team_id: UUID`
    - `title` (1 to 200 chars) and `description` (up to 20000 chars)
    - `type` and `priority`, as `Literal` types built from the constants in `rules.py`
    - `requires_approval: bool = False`
    - `due_at: datetime | None = None`
  - `create_item(conn, user_id, data: ItemCreate) -> dict`. If the user isn't in the team it raises 404. On success it records a `created` activity.
  - `get_item(conn, item_id, user_id) -> dict`.
  - `list_activity(conn, item_id, user_id, before_id: int | None = None, limit: int = 50) -> dict` returns `{"entries": [{id, kind, actor: {id, name}, payload, created_at}], "next_before_id": int | None}`, newest first.
  - Routes:
    - `POST /api/items` returns 201.
    - `GET /api/items/{id}`.
    - `GET /api/items/{id}/activity?before_id=&limit=` (limit at most 100).

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Add these checks to `check.py`:
  - `check_create_open_item`: the item has status `open`, version 1 and exactly one `created` activity.
  - `check_requires_approval_starts_awaiting`
  - `check_outsider_create_and_get_404`: `expect_error(..., 404, "NOT_FOUND")`.
  - **`check_membership_removed_404`**: delete the membership row, then `get_item` raises 404.
  - **`check_malformed_id_404`**: `get_item(conn, "abc", u)` raises 404 `NOT_FOUND`.
  - **`check_activity_pages_of_50`**: insert 120 activity rows. Following `next_before_id` gives pages of 50, 50 and 20 with no duplicate ids, and the last page's `next_before_id` is None.
  - `check_member_actions_on_unassigned_open`: `allowed_actions` contains `claim` and doesn't contain `start` or `assign`.
- [ ] **Step 3:** Run `python scripts/check.py`. Expected: every check passes.
- [ ] **Step 4:** Commit with `feat: create/read work items with team-scoped access and timeline`.

---

### Task 5: Safe retries with idempotency keys

**Files:**
- Create: `app/idempotency.py`
- Modify: `app/routes.py`, `scripts/check.py`

**Interfaces:**
- Produces `run_idempotent(conn, user_id: str, key: str | None, request_hash: str, fn: Callable[[], tuple[int, dict]]) -> tuple[int, dict, bool]`, returning `(status, body, replayed)`. Everything happens inside the request's transaction:
  1. If `key` is missing or longer than 200 chars, raise 400 `IDEMPOTENCY_KEY_REQUIRED`.
  2. Run `INSERT INTO idempotency_keys (user_id, key, request_hash, status_code, response) VALUES (%s,%s,%s,0,'null') ON CONFLICT DO NOTHING RETURNING key`. A concurrent duplicate waits here until the first transaction finishes.
  3. If nothing was inserted, select the existing row:
     - a different `request_hash` raises 422 `IDEMPOTENCY_KEY_REUSED`
     - otherwise return the stored status and body with `replayed=True`
  4. Otherwise run `fn()`, store its status and body in the row, and return `replayed=False`. If `fn` raises, the transaction rolls back, key row included, so the client can retry with the same key.
- `request_hash = sha256(f"{method} {path} {json.dumps(body, sort_keys=True, default=str)}")`.
- Every `POST` under `/api/items` (create, comment, claim, assign, transition) reads the `Idempotency-Key` header and goes through `run_idempotent`. A replayed response carries the header `Idempotent-Replayed: true`.

- [ ] **Step 1:** Implement, and wrap the create route.
- [ ] **Step 2:** Add these checks:
  - `check_same_key_twice_one_item`: the second call returns `replayed=True` with the same body, and exactly one item exists.
  - `check_same_key_different_body_422`
  - `check_missing_key_400`
  - `check_keys_are_per_user`
  - `check_failed_call_does_not_burn_key`: `fn` raises inside a transaction that rolls back, then the same key with a working `fn` succeeds.
  - `check_concurrent_same_key_creates_one`: 8 threads use the same key. Afterwards exactly one item exists, and all 8 bodies have the same `id`.
- [ ] **Step 3:** Run `python scripts/check.py`. Expected: every check passes.
- [ ] **Step 4:** Commit with `feat: idempotency keys for item mutations`.

---

### Task 6: Edits with version checks, comments

**Files:**
- Modify: `app/items.py`, `app/routes.py`, `scripts/check.py`

**Interfaces:**
- Produces:
  - `update_item(conn, item_id, user_id, expected_version: int, changes: dict) -> dict`.
    - Allowed keys: `title`, `description`, `priority`, `due_at`. Requires the `edit` permission.
    - Fields whose value hasn't changed are dropped. If nothing is left, return the current DTO without bumping the version or writing activity.
    - Otherwise run `UPDATE work_items SET …, version = version + 1, updated_at = now() WHERE id = %s AND version = %s RETURNING *`.
    - If no row comes back, raise 409 `STALE_VERSION` with `details={"current": <item_dto>, "changed_by": <name of the latest activity's actor>}`.
    - If it succeeds, record one `updated` activity with `payload={"changes": {field: {"from": old, "to": new}}}`.
  - `add_comment(conn, item_id, user_id, body: str) -> dict`. The body is 1 to 10000 chars, and the caller needs the `comment` permission. It returns the activity entry.
  - Routes:
    - `PATCH /api/items/{id}` with body `{expected_version, title?, description?, priority?, due_at?}`.
    - `POST /api/items/{id}/comments` with body `{body}`. It is idempotent and returns 201.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Add these checks:
  - `check_patch_bumps_version_and_logs_changed_fields_only`
  - `check_patch_old_version_409_db_unchanged`: `details["current"]["version"]` is the latest version.
  - `check_concurrent_patches_same_version_one_wins`: 2 threads. One succeeds and one raises 409.
  - `check_noop_patch_no_activity`
  - `check_patch_cancelled_item_403`
- [ ] **Step 3:** Run `python scripts/check.py`. Expected: every check passes.
- [ ] **Step 4:** Commit with `feat: versioned edits and comments`.

---

### Task 7: Claim, assign, workflow transitions

**Files:**
- Modify: `app/items.py`, `app/routes.py`, `scripts/check.py`

**Interfaces:**
- Consumes: `next_status`, `REASON_REQUIRED` and `can` from Task 2; `load_item` and `record_activity` from Task 4; `run_idempotent` from Task 5.
- Produces three functions, each returning the item DTO. Each has an idempotent `POST` route.
  - `claim_item(conn, item_id, user_id) -> dict`:
    - If the item is already assigned to the caller, return it unchanged. Otherwise the caller needs the `claim` permission.
    - Claim with a single statement: `UPDATE work_items SET assignee_id = %s, version = version + 1 WHERE id = %s AND assignee_id IS NULL AND status NOT IN ('done','cancelled') RETURNING *`.
    - If no row comes back, raise 409 `ALREADY_CLAIMED` with `details={"assignee": {id, name}}`.
    - Record an `assigned` activity with `{"from": None, "to": user_id}`.
    - Claim takes no `expected_version`, so an unrelated edit doesn't block it.
  - `assign_item(conn, item_id, user_id, assignee_id: str | None, expected_version: int) -> dict`:
    - Requires the `assign` permission.
    - If the assignee isn't in the item's team, raise 422 `INVALID_ASSIGNEE`.
    - Version-guarded the same way as Task 6 (409 `STALE_VERSION`).
    - Records an `assigned` activity with `{from, to}`.
  - `transition_item(conn, item_id, user_id, action: str, reason: str | None, expected_version: int) -> dict`:
    - Loads the item with `for_update=True`, then checks, in this order:
      1. version mismatch → 409 `STALE_VERSION`
      2. `next_status` is None → 422 `INVALID_TRANSITION` with `details={"from", "action"}`
      3. no permission → 403
      4. reason missing for an action in `REASON_REQUIRED` → 400 `REASON_REQUIRED`
      5. `start` with no assignee → 422 `ASSIGNEE_REQUIRED`
    - Records a `status_changed` activity with `{from, to, action, reason}`.
  - Routes:
    - `POST /api/items/{id}/claim`
    - `POST /api/items/{id}/assign` with body `{assignee_id, expected_version}`
    - `POST /api/items/{id}/transitions` with body `{action, reason?, expected_version}`

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** Add these checks:
  - **`check_ten_concurrent_claims_one_winner`**: 10 member users each try to claim in their own thread and transaction. Exactly 1 succeeds; 9 get 409 `ALREADY_CLAIMED` naming the winner; there is exactly one `assigned` activity.
  - `check_claim_own_item_noop`
  - **`check_assign_to_outsider_422_unchanged`**
  - `check_member_cannot_assign_403`
  - `check_open_to_complete_422_invalid_transition`
  - `check_start_without_assignee_422`
  - `check_block_without_reason_400`
  - `check_creator_lead_cannot_approve_other_lead_can`
  - `check_non_assignee_member_cannot_complete`
  - `check_stale_transition_409`
- [ ] **Step 3:** Run `python scripts/check.py`. Expected: every check passes.
- [ ] **Step 4:** Commit with `feat: atomic claim, assignment and enforced workflow transitions`.

---

### Task 8: List, search, attention views, overview, seed data

**Files:**
- Create: `app/listing.py`, `app/seed.py`
- Modify: `app/routes.py`, `scripts/check.py`

**Interfaces:**
- Produces:
  - `view_condition(view: str, user_id: str) -> tuple[str, list]` returns a SQL fragment and its parameters for each attention view. An unknown view raises 400 `INVALID_VIEW`.
  - `list_items(conn, user_id, *, view=None, team_id=None, status: list[str] = (), priority: list[str] = (), type: list[str] = (), assignee: str | None = None, q: str | None = None, sort: str | None = None, page: int = 1, page_size: int = 25) -> dict` returns `{"items", "page", "page_size", "has_more"}`.
    - The query always includes `team_id IN (SELECT team_id FROM memberships WHERE user_id = %s)`.
    - `assignee` is `"me"`, `"unassigned"` or a user id.
    - `q` is ignored if it's blank after `strip()`. Otherwise it filters with `search @@ websearch_to_tsquery('english', %s)`, and when `sort` is None the results are ordered by `ts_rank` descending.
    - `sort` options:
      - `priority` (the default without `q`): `priority, last_activity_at desc, id`
      - `updated`: `last_activity_at desc, id`
      - `created`: `created_at desc, id`
    - Paging uses `LIMIT page_size + 1 OFFSET (page-1)*page_size`; `has_more` is true when that extra row comes back. No `COUNT(*)`.
    - Each listed item is the DTO without `description` and `allowed_actions`.
  - `overview_counts(conn, user_id) -> dict` returns `{"mine", "unassigned_urgent", "awaiting_my_approval", "overdue", "stale"}`. The counts are built from `view_condition`, so they always match what the lists show.
  - Routes:
    - `GET /api/items?view&team_id&status&priority&type&assignee&q&sort&page&page_size`. Status, priority and type are comma-separated; `page_size` is at most 100.
    - `GET /api/overview`.
  - `python -m app.seed --items 20000` re-applies the schema and creates:
    - 6 teams and 40 users, all with the password `password`.
    - These named users:
      - `lead@newtonite.test`: lead of Payments and Platform
      - `lead2@newtonite.test`: lead of Payments
      - `member@newtonite.test`: member of Payments and Platform
      - `outsider@newtonite.test`: member of Support only
    - Items with a mix of status, priority, type, age and due dates, so some are stale or overdue. Each item gets a `created` activity row. Rows are inserted with `executemany` in batches of 1000.

- [ ] **Step 1:** Implement `listing.py`, the routes and `seed.py`.
- [ ] **Step 2:** Add these checks:
  - `check_list_never_shows_other_teams`
  - `check_status_and_priority_filters`
  - `check_assignee_me_and_unassigned`
  - `check_pages_and_has_more`: 60 items at page size 25 give pages of 25, 25 and 10, and the last has `has_more=False`.
  - `check_search_stems`: searching `payments` finds an item titled "payment".
  - **`check_search_garbage_ok`**: the queries `foo & (bar`, `"` and `"   "` all return without error.
  - `check_stale_view`: backdate `last_activity_at` by 4 days and the item appears in the view.
  - `check_awaiting_my_approval_excludes_own_and_non_lead_teams`
  - `check_overview_counts_match_lists`
  - `check_unknown_view_400`
- [ ] **Step 3:** Run `python scripts/check.py`. Expected: every check passes.
- [ ] **Step 4:** Check scale:
  - Run `python -m app.seed --items 20000`.
  - In pgAdmin, run `EXPLAIN ANALYZE` on the default list query for `member@newtonite.test`.
  - Expected: it uses the `(team_id, status, priority, …)` index and finishes in well under 50 ms.
  - Note the result for ENGINEERING_DECISIONS.
  - Then run `python scripts/check.py` again. The checks should still pass alongside the seeded data.
- [ ] **Step 5:** Commit with `feat: filtered list, search, attention overview and seed data`.

---

### Task 9: Frontend (plain HTML + JavaScript)

**Files:**
- Create: `static/login.html`, `static/index.html`, `static/new.html`, `static/item.html`, `static/app.js`, `static/style.css`

**Interfaces:**
- Consumes: the API from Tasks 3–8.
- Produces, in `app.js`:
  - `class ApiError extends Error { status; code; details }`.
  - `api(method, path, body = null, { idempotencyKey } = {}) -> Promise<any>`:
    - sends JSON and parses the error envelope into an `ApiError`
    - a network failure becomes `ApiError(0, "NETWORK")`
    - a 401 redirects to `/login.html`
  - `newKey() -> string`, which returns `crypto.randomUUID()`.
    - A key is created once per click and reused if the user resubmits after a network error.
    - Submit buttons are disabled while their request is pending.
  - One init function per page (`initLogin`, `initOverview`, `initNew`, `initItem`), picked by the `data-page` attribute on `<body>`.
- Pages:
  - **login.html** posts to `/api/login`, then goes to `/`.
  - **index.html**:
    - Five attention tiles from `/api/overview`. Clicking a tile sets `?view=`.
    - A filter bar with team, status, priority, assignee, a search box (debounced 300 ms) and sort. Its state lives in the URL query string, so links can be shared and survive a reload.
    - A results table with Previous/Next buttons driven by `page` and `has_more`. Rows link to `item.html?id=`.
  - **new.html** has the create form. The team dropdown comes from `/api/me`. On success it goes to the item page.
  - **item.html**:
    - Shows the item's fields and its timeline, 50 at a time, with a "Load older" button using `next_before_id`. Below that is a comment box.
    - Action buttons are drawn only from `allowed_actions`. Actions that need a reason ask for one.
    - **Polling:** re-fetch every 10 s and when the tab regains focus. If the version changed while the edit form has unsaved input, show the banner "Changed by someone else — review" and leave the input alone.
    - **Stale save** (409 `STALE_VERSION`): keep the draft and show each field as "yours" next to "current" (from `details.current`), with two buttons:
      - *Keep mine* resends with `expected_version = current.version`.
      - *Discard mine* drops the draft.
    - **Optimistic claim:** show "Assigned to you" immediately.
      - On 409 `ALREADY_CLAIMED`, revert and show "Already claimed by {details.assignee.name}".
      - On any other error, revert and show the message.
      - Re-fetch the item afterwards either way.

- [ ] **Step 1:** Implement the pages.
- [ ] **Step 2:** Run `uvicorn app.main:app --reload` and check by hand on the seeded data, using one normal and one private browser window:
  - Log in as `member@` in one window and `lead2@` in the other. Both claim the same unassigned Payments item. One succeeds and the other sees "Already claimed by …".
  - Both edit the same title. The second save shows the yours/current comparison.
  - As `lead@`, open an item that `lead@` created. There is no Approve button, and calling the API directly from `/docs` returns 403.
  - Set DevTools to Offline, click Comment, go back online and click again. Exactly one comment appears.
  - `outsider@` opening a Payments item URL gets "not found".
  - Each overview tile's count matches the list it opens.
- [ ] **Step 3:** Commit with `feat: plain JS frontend with overview, filters, conflict handling and optimistic claim`.

---

### Task 10: Documentation and final check

**Files:**
- Create: `README.md`, `ENGINEERING_DECISIONS.md`

- [ ] **Step 1:** Write `README.md`. It covers:
  - **Prerequisites:** Python 3.11 and the local PostgreSQL with pgAdmin.
  - **Setup:**
    - create `newtonite` in pgAdmin
    - copy `.env.example` to `.env`
    - `python -m venv .venv`
    - `.venv\Scripts\pip install -r requirements.txt`
    - `python -m app.seed --items 20000`
  - **Run:** `uvicorn app.main:app --reload`, then open `http://localhost:8000`.
  - **Checks:** `python -m unittest` and `python scripts/check.py`.
  - The demo accounts.
  - A short architecture sketch (browser → FastAPI → PostgreSQL).
  - The role/permission table and the workflow diagram.
  - **Known limitations:**
    - polling instead of push
    - no notifications or background jobs
    - no attachments
    - no admin UI for teams and users
    - password login only
    - CSRF protection relies on `SameSite=Lax` plus JSON bodies
    - offset paging can show duplicates or skip items if data changes between pages
    - idempotency keys are never cleaned up
    - the behaviour checks are a script rather than a test framework
    - the frontend is verified by a manual checklist
- [ ] **Step 2:** Write `ENGINEERING_DECISIONS.md` with five decisions, each covering *Decision / Why / Trade-off / At larger scale*:
  1. PostgreSQL with plain SQL and one transaction per request, with an append-only activity table as the history.
  2. Version numbers for edits and transitions, and a single conditional `UPDATE` for claim, including why claim doesn't use a version.
  3. Idempotency keys stored in the same transaction as the change, so concurrent duplicates queue up on the primary key.
  4. A single pure rules module, where the server returns `allowed_actions`, non-members get 404, and creators can't approve their own items.
  5. What was deliberately left out (async jobs, push updates, a frontend framework, a test framework, keyset paging) and when each would become worth adding.

  Include the `EXPLAIN ANALYZE` result from Task 8.
- [ ] **Step 3:** Final check:
  - `python -m unittest -v` and `python scripts/check.py` both pass completely.
  - Re-run the seed, follow the README from the top, and log in as each demo account.
- [ ] **Step 4:** Commit with `docs: README, engineering decisions and known limitations`.
