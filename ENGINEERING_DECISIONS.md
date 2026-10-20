# Engineering decisions

Five decisions that shaped the system, what they cost, and what I would change
as it grows. The guiding constraint was one working day: I chose a small system
whose risky behaviour is deliberate and checked, over a wider one with gaps.

---

## 1. PostgreSQL as the single source of truth, plain SQL, one transaction per request

**Decision.** All state lives in PostgreSQL. Each HTTP request gets one pooled
connection and one transaction (`app/db.py`); the item change, its `activity`
row and its idempotency record commit together or roll back together. Queries
are plain SQL through psycopg 3 — no ORM. History is an append-only `activity`
table (`created`, `updated` with per-field from/to, `assigned`,
`status_changed` with reason, `comment`).

**Why.** The hard parts of this problem (claim races, stale writes, duplicate
requests) are all solved by the database's guarantees: row locks, conditional
updates, unique constraints, atomic commits. Plain SQL keeps those mechanisms
visible in review — `WHERE version = %s`, `ON CONFLICT DO NOTHING`, `FOR UPDATE`
are right there in `app/items.py`. Writing history in the same transaction means
"important actions should not silently disappear" is a structural guarantee, not
a convention: there is no code path that changes an item without logging it.

**Trade-off.** More hand-written SQL than an ORM would need, and the schema is
a single re-runnable `schema.sql` rather than versioned migrations — fine for a
first version, not for a live system. The activity table is not event sourcing:
`work_items` holds current state and `activity` explains it, so the two could in
principle drift if someone wrote SQL by hand.

**At larger scale.** Add a migration tool (Alembic or plain numbered SQL files)
before the first production deploy. Partition `activity` by month once it gets
into the tens of millions of rows; it's only ever read per item, newest first,
which partitions well.

---

## 2. Concurrency: version numbers for edits, a conditional update for claiming

**Decision.** Every work item has a `version`. Edits, assignments and status
changes must send `expected_version`; the update runs
`... WHERE id = %s AND version = %s`, and if no row matches the API returns
**409 `STALE_VERSION`** with the current item and who changed it last.
Status changes additionally lock the row (`SELECT ... FOR UPDATE`) because they
validate the current status before writing.

Claiming is different: it's a single statement,
`UPDATE ... SET assignee_id = me WHERE id = %s AND assignee_id IS NULL`.
The first statement to run wins; everyone else gets **409 `ALREADY_CLAIMED`**
with the winner's name. Claim deliberately does *not* take a version.

**Why.** Optimistic concurrency fits the usage: conflicts on the same item are
rare, and when they happen the right answer is to *show* the user what changed,
not silently overwrite (last-write-wins) or hold locks while someone types.
The UI keeps the user's draft and shows "yours vs current" side by side, with
*Keep mine* / *Discard mine*.

For claiming, the question users care about is "is anyone already on this?",
not "has anything about this item changed?". If claim required a version, a
harmless priority edit would make a claim fail. The conditional update answers
exactly the right question, and the check `scripts/check.py ::
check_ten_concurrent_claims_one_winner` races 10 threads to prove there is
exactly one winner and exactly one history entry.

**Trade-off.** Clients must carry versions and handle 409s — more frontend work
than last-write-wins. Comments don't bump the version (they don't change the
item), so commenting never causes someone else's edit to be rejected.

**At larger scale.** Field-level merging (two people editing *different* fields
both succeed) would reduce conflicts further; today the UI shows "your edits
don't conflict" and lets you resubmit in one click.

---

## 3. Safe retries with idempotency keys stored in the same transaction

**Decision.** Every `POST` that creates or changes something requires an
`Idempotency-Key` header. `app/idempotency.py` inserts `(user_id, key,
request_hash)` with `ON CONFLICT DO NOTHING` *inside the request's transaction*,
runs the operation, and stores the response. A repeat with the same key returns
the stored response (header `Idempotent-Replayed: true`); the same key with a
different body is rejected (422). The frontend creates one key per user intent
and reuses it if the user retries after a network error.

**Why.** "A user repeating an action because they are unsure whether the first
request succeeded" is the most common real-world failure here: double clicks,
flaky Wi-Fi, a timeout after the server actually committed. Putting the key row
in the same transaction gives two properties for free:

* two simultaneous requests with one key queue on the primary key; the second
  waits for the first to commit, then returns its stored response
  (`check_concurrent_same_key_creates_one`: 8 threads → 1 item);
* if the operation fails, the key row rolls back too, so the retry can succeed
  (`check_failed_call_does_not_burn_key`).

**Trade-off.** Clients must send a header; keys accumulate forever (no cleanup
yet). PATCH doesn't use keys — the version check already makes a repeated edit
harmless (the replay becomes a no-op or a visible 409).

**At larger scale.** A nightly job deleting keys older than 24–48 hours.

---

## 4. One pure rules module; the server decides and tells the UI what's allowed

**Decision.** The workflow (which status changes are legal) and the permission
model (who may do what) live in `app/rules.py` as pure functions with no
database access. Every API operation calls them; every item response includes
`allowed_actions`, computed by the same function, and the UI renders only those
buttons. Roles are per team (`member`, `lead`). Key rules:

* Not in the item's team → **404**, not 403: outsiders can't learn what exists.
* Members can work items assigned to them; leads can act on any item in their
  team and are the only ones who can assign, cancel, approve and reject.
* **The creator of an item can never approve or reject it** (separation of
  duties) — even if they're a lead.
* Membership is read from the database on every request, so removing someone
  from a team takes effect on their next click (`check_membership_removed_404`).

**Why.** Authorization must be enforced by the server, and the UI must not
drift from it. One function serving both purposes means there's a single place
to read, test and change the rules, and the tests for it (`tests/test_rules.py`)
need no database.

**Trade-off.** A fixed two-role model. It doesn't cover cross-team observers
(e.g. management wanting read-only access to everything) or custom per-team
workflows.

**At larger scale.** Add a read-only `viewer` role and an org-wide auditor role;
if teams need different workflows, move `TRANSITIONS` into data per team.

---

## 5. What I deliberately did not build (and when I would)

| Not built | Why not now | When it becomes worth it |
|---|---|---|
| Background jobs / notifications | Adds a queue, retries, dedupe and failure modes; the attention views already answer "what needs me?" | When people need to be told outside the app (email/Slack). I'd use a transactional outbox table drained by a worker with `FOR UPDATE SKIP LOCKED`, with idempotent handlers. |
| Live push (WebSockets/SSE) | Polling every 10 s + version checks is correct, just slightly delayed | When users report seeing stale screens, or for incident rooms |
| Frontend framework / build step | A few pages of plain JS are easy to read and run anywhere | When the UI grows past a handful of screens or needs shared components |
| Keyset (cursor) pagination for lists | Page numbers are simple and fast at this size | When users page deep into large result sets, or rows shifting between pages becomes a complaint |
| Test framework (pytest) and a separate test DB | Kept tooling minimal by request; `scripts/check.py` isolates its own data | As soon as there's CI or more than one developer |

### Does it stay fast as data grows?

Measured with the seed data (20,000 items; `member@` can see 6,793 of them),
`EXPLAIN ANALYZE` on the queries the overview uses:

| Query | Plan | Time |
|---|---|---|
| Default list (my teams, priority order, page 1) | bitmap index scan on `(team_id, status, priority, last_activity_at)` | 28 ms |
| Status filter | same index | 23 ms |
| Full-text search "refund payment" | GIN index on the generated `search` column | 6 ms |
| `view=mine` | index on `(assignee_id, status)` | 3 ms |
| All five overview counts | one pass with `count(*) FILTER (...)` | 14 ms |

Lists are always limited to the caller's teams and to one page (≤ 100 rows);
the browser never loads more than that. Search uses `websearch_to_tsquery`, which
accepts any user text, so stray quotes or operators can't produce a 500.
