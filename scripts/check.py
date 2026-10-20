"""Behaviour checks against the real database: python scripts/check.py

Each run creates its own uniquely named team(s) and users, runs every
`check_*` function in this file, prints PASS/FAIL per check, and deletes
everything it created (even when a check fails). Exit code 1 if any failed.
"""
import sys
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATABASE_URL  # noqa: E402
from app.db import create_pool  # noqa: E402

RUN = uuid.uuid4().hex[:8]
pool = create_pool(DATABASE_URL)
_counter = 0


def _next() -> int:
    global _counter
    _counter += 1
    return _counter


@contextmanager
def tx():
    """A pooled connection in its own transaction (commit on success)."""
    with pool.connection() as conn:
        yield conn


def sql(query: str, params=()) -> list[dict]:
    with tx() as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else []


# ---- fixtures -------------------------------------------------------------

def team() -> dict:
    return sql("INSERT INTO teams (name) VALUES (%s) RETURNING *", (f"check-{RUN}-{_next()}",))[0]


_PASSWORD_HASH = None


def user(role: str | None = None, team_id=None, name: str | None = None) -> dict:
    from app.auth import hash_password

    global _PASSWORD_HASH
    _PASSWORD_HASH = _PASSWORD_HASH or hash_password("password")  # scrypt is slow; hash once per run
    n = _next()
    row = sql(
        "INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s) RETURNING *",
        (f"{RUN}-{n}@check.test", name or f"Check User {n}", _PASSWORD_HASH),
    )[0]
    if role:
        add_member(row["id"], team_id, role)
    return row


def add_member(user_id, team_id, role: str) -> None:
    sql("INSERT INTO memberships (user_id, team_id, role) VALUES (%s, %s, %s)", (user_id, team_id, role))


def item(created_by: dict, team_id, **overrides) -> dict:
    fields = {
        "team_id": team_id, "title": f"Check item {_next()}", "description": "",
        "type": "task", "priority": "medium", "status": "open",
        "requires_approval": False, "created_by": created_by["id"],
    }
    fields.update(overrides)
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    return sql(f"INSERT INTO work_items ({cols}) VALUES ({marks}) RETURNING *", tuple(fields.values()))[0]


def expect_error(fn, status: int, code: str):
    from app.errors import AppError

    try:
        fn()
    except AppError as e:
        assert (e.status, e.code) == (status, code), f"expected {status} {code}, got {e.status} {e.code}"
        return e
    raise AssertionError(f"expected AppError {status} {code}, nothing raised")


def cleanup() -> None:
    sql("DELETE FROM teams WHERE name LIKE %s", (f"check-{RUN}-%",))
    sql("DELETE FROM users WHERE email LIKE %s", (f"{RUN}-%@check.test",))


# ---- Task 1: schema -------------------------------------------------------

def check_schema():
    names = {r["table_name"] for r in sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    expected = {"users", "teams", "memberships", "work_items", "activity", "idempotency_keys"}
    assert expected <= names, f"missing tables: {expected - names}"
    rows = sql("SELECT unnest(enum_range(NULL::item_priority))::text AS p")
    assert [r["p"] for r in rows] == ["urgent", "high", "medium", "low"]


# ---- Task 3: auth ---------------------------------------------------------

def check_password_hash_roundtrip():
    from app.auth import hash_password, verify_password

    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert h != hash_password("s3cret"), "hashes must be salted"


# ---- Task 4: create/read, access, timeline --------------------------------

def create(u: dict, team_id, **kw) -> dict:
    from app.items import ItemCreate, create_item

    data = ItemCreate(**{"team_id": team_id, "title": "Checked item", "type": "task", "priority": "medium", **kw})
    with tx() as conn:
        return create_item(conn, u["id"], data)


def call(fn, *args, **kw):
    """Run an app.items function in its own transaction."""
    with tx() as conn:
        return fn(conn, *args, **kw)


def activity_kinds(item_id) -> list[str]:
    return [r["kind"] for r in sql("SELECT kind FROM activity WHERE work_item_id = %s ORDER BY id", (item_id,))]


def check_create_open_item():
    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"])
    assert it["status"] == "open" and it["version"] == 1, it
    assert activity_kinds(it["id"]) == ["created"]


def check_requires_approval_starts_awaiting():
    t = team()
    m = user("member", t["id"])
    assert create(m, t["id"], requires_approval=True)["status"] == "awaiting_approval"


def check_outsider_cannot_see_other_peoples_items():
    from app.items import get_item

    t = team()
    m = user("member", t["id"])
    outsider = user()
    it = create(m, t["id"])
    expect_error(lambda: call(get_item, it["id"], outsider["id"]), 404, "NOT_FOUND")


def check_membership_removed_404():
    from app.items import get_item

    t = team()
    creator, m = user("member", t["id"]), user("member", t["id"])
    it = create(creator, t["id"])
    call(get_item, it["id"], m["id"])  # works while a member
    sql("DELETE FROM memberships WHERE user_id = %s", (m["id"],))
    expect_error(lambda: call(get_item, it["id"], m["id"]), 404, "NOT_FOUND")


def check_malformed_id_404():
    from app.items import get_item

    m = user()
    expect_error(lambda: call(get_item, "abc", m["id"]), 404, "NOT_FOUND")


def check_activity_pages_of_50():
    from app.items import list_activity

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"])  # 1 'created' entry
    sql("""INSERT INTO activity (work_item_id, actor_id, kind, payload)
           SELECT %s, %s, 'comment', jsonb_build_object('body', 'c' || g) FROM generate_series(1, 119) g""",
        (it["id"], m["id"]))
    seen, sizes, before = [], [], None
    while True:
        page = call(list_activity, it["id"], m["id"], before)
        sizes.append(len(page["entries"]))
        seen += [e["id"] for e in page["entries"]]
        before = page["next_before_id"]
        if before is None:
            break
    assert sizes == [50, 50, 20], sizes
    assert len(seen) == len(set(seen)) == 120


def check_member_actions_on_unassigned_open():
    t = team()
    m = user("member", t["id"])
    acts = create(m, t["id"])["allowed_actions"]
    assert "claim" in acts and "start" not in acts and "assign" not in acts, acts


# ---- Task 5: idempotency --------------------------------------------------

def idem_create(u: dict, team_id, key: str | None, title: str = "Idem item"):
    from app.idempotency import request_hash, run_idempotent
    from app.items import ItemCreate, create_item

    data = ItemCreate(team_id=team_id, title=title, type="task", priority="medium")
    h = request_hash("POST", "/api/items", data.model_dump(mode="json"))
    with tx() as conn:
        return run_idempotent(conn, u["id"], key, h, lambda: (201, create_item(conn, u["id"], data)))


def item_count(team_id) -> int:
    return sql("SELECT count(*) AS n FROM work_items WHERE team_id = %s", (team_id,))[0]["n"]


def check_same_key_twice_one_item():
    t = team()
    m = user("member", t["id"])
    s1, b1, r1 = idem_create(m, t["id"], "k1")
    s2, b2, r2 = idem_create(m, t["id"], "k1")
    assert (s1, r1, s2, r2) == (201, False, 201, True)
    assert b1 == b2 and item_count(t["id"]) == 1


def check_same_key_different_body_422():
    t = team()
    m = user("member", t["id"])
    idem_create(m, t["id"], "k1", title="First")
    expect_error(lambda: idem_create(m, t["id"], "k1", title="Second"), 422, "IDEMPOTENCY_KEY_REUSED")


def check_missing_key_400():
    t = team()
    m = user("member", t["id"])
    expect_error(lambda: idem_create(m, t["id"], None), 400, "IDEMPOTENCY_KEY_REQUIRED")
    assert item_count(t["id"]) == 0


def check_keys_are_per_user():
    t = team()
    a, b = user("member", t["id"]), user("member", t["id"])
    idem_create(a, t["id"], "shared")
    idem_create(b, t["id"], "shared")
    assert item_count(t["id"]) == 2


def check_failed_call_does_not_burn_key():
    from app.idempotency import run_idempotent

    t = team()
    m = user("member", t["id"])

    def boom():
        raise RuntimeError("simulated failure")

    try:
        with tx() as conn:
            run_idempotent(conn, m["id"], "retry-me", "h", boom)
    except RuntimeError:
        pass
    status, _, replayed = idem_create(m, t["id"], "retry-me")
    assert (status, replayed) == (201, False) and item_count(t["id"]) == 1


def check_concurrent_same_key_creates_one():
    t = team()
    m = user("member", t["id"])
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda _: idem_create(m, t["id"], "double-click"), range(8)))
    ids = {body["id"] for _, body, _ in results}
    assert len(ids) == 1, ids
    assert item_count(t["id"]) == 1
    assert sum(1 for _, _, replayed in results if not replayed) == 1


# ---- Task 6: versioned edits, comments ------------------------------------

def outcome(fn):
    """('ok', result) or (error code, AppError) — for racing threads."""
    from app.errors import AppError

    try:
        return "ok", fn()
    except AppError as e:
        return e.code, e


def check_patch_bumps_version_and_logs_changed_fields_only():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"], title="Old", priority="medium")
    out = call(update_item, it["id"], m["id"], 1, {"title": "New", "priority": "medium"})
    assert out["version"] == 2 and out["title"] == "New"
    payload = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (it["id"],))[0]["payload"]
    assert payload == {"changes": {"title": {"from": "Old", "to": "New"}}}, payload


def check_patch_old_version_409_db_unchanged():
    from app.items import update_item

    t = team()
    m = user("member", t["id"], name="First Editor")
    it = create(m, t["id"], title="Original")
    call(update_item, it["id"], m["id"], 1, {"title": "Second"})
    err = expect_error(lambda: call(update_item, it["id"], m["id"], 1, {"title": "Stale write"}), 409, "STALE_VERSION")
    assert err.details["current"]["version"] == 2 and err.details["current"]["title"] == "Second"
    assert err.details["changed_by"]["name"] == "First Editor"
    assert sql("SELECT title FROM work_items WHERE id = %s", (it["id"],))[0]["title"] == "Second"


def check_concurrent_patches_same_version_one_wins():
    from app.items import update_item

    t = team()
    a, b = user("member", t["id"]), user("member", t["id"])
    it = create(a, t["id"])
    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(
            lambda u: outcome(lambda: call(update_item, it["id"], u["id"], 1, {"title": f"by {u['id']}"})), [a, b]))
    codes = sorted(code for code, _ in results)
    assert codes == ["STALE_VERSION", "ok"], codes
    assert sql("SELECT version FROM work_items WHERE id = %s", (it["id"],))[0]["version"] == 2


def check_noop_patch_no_activity():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"], title="Same")
    out = call(update_item, it["id"], m["id"], 1, {"title": "Same"})
    assert out["version"] == 1 and activity_kinds(it["id"]) == ["created"]


def check_patch_cancelled_item_403():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = item(m, t["id"], status="cancelled")
    expect_error(lambda: call(update_item, it["id"], m["id"], 1, {"title": "x"}), 403, "FORBIDDEN")


# ---- Task 7: claim, assign, transitions -----------------------------------

def check_ten_concurrent_claims_one_winner():
    from app.items import claim_item

    t = team()
    creator = user("member", t["id"])
    it = create(creator, t["id"])
    members = [user("member", t["id"]) for _ in range(10)]
    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(lambda u: outcome(lambda: call(claim_item, it["id"], u["id"])), members))
    winners = [r for code, r in results if code == "ok"]
    losers = [e for code, e in results if code == "ALREADY_CLAIMED"]
    assert len(winners) == 1 and len(losers) == 9, [c for c, _ in results]
    winner_id = winners[0]["assignee"]["id"]
    assert all(e.details["assignee"]["id"] == winner_id for e in losers)
    assert activity_kinds(it["id"]).count("assigned") == 1


def check_claim_own_item_noop():
    from app.items import claim_item

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"])
    call(claim_item, it["id"], m["id"])
    again = call(claim_item, it["id"], m["id"])
    assert again["version"] == 2 and activity_kinds(it["id"]).count("assigned") == 1


def check_assign_to_outsider_422_unchanged():
    from app.items import assign_item

    t = team()
    lead = user("lead", t["id"])
    outsider = user()
    it = create(lead, t["id"])
    expect_error(lambda: call(assign_item, it["id"], lead["id"], outsider["id"], 1), 422, "INVALID_ASSIGNEE")
    row = sql("SELECT assignee_id, version FROM work_items WHERE id = %s", (it["id"],))[0]
    assert row["assignee_id"] is None and row["version"] == 1


def check_member_cannot_assign_403():
    from app.items import assign_item

    t = team()
    m, other = user("member", t["id"]), user("member", t["id"])
    it = create(m, t["id"])
    expect_error(lambda: call(assign_item, it["id"], m["id"], other["id"], 1), 403, "FORBIDDEN")


def transition(it, u, action, version, reason=None):
    from app.items import transition_item

    return call(transition_item, it["id"], u["id"], action, reason, version)


def check_open_to_complete_422_invalid_transition():
    t = team()
    lead = user("lead", t["id"])
    it = create(lead, t["id"])
    expect_error(lambda: transition(it, lead, "complete", 1, "done"), 422, "INVALID_TRANSITION")


def check_start_without_assignee_422():
    t = team()
    lead = user("lead", t["id"])
    it = create(lead, t["id"])
    expect_error(lambda: transition(it, lead, "start", 1), 422, "ASSIGNEE_REQUIRED")


def check_block_without_reason_400():
    t = team()
    m = user("member", t["id"])
    it = item(m, t["id"], status="in_progress", assignee_id=m["id"])
    expect_error(lambda: transition(it, m, "block", 1, "   "), 400, "REASON_REQUIRED")
    assert transition(it, m, "block", 1, "Waiting on bank")["status"] == "blocked"


def check_creator_lead_cannot_approve_other_lead_can():
    t = team()
    lead1, lead2 = user("lead", t["id"]), user("lead", t["id"])
    it = create(lead1, t["id"], requires_approval=True)
    expect_error(lambda: transition(it, lead1, "approve", 1), 403, "FORBIDDEN")
    assert transition(it, lead2, "approve", 1)["status"] == "open"


def check_non_assignee_member_cannot_complete():
    t = team()
    owner, other = user("member", t["id"]), user("member", t["id"])
    it = item(owner, t["id"], status="in_progress", assignee_id=owner["id"])
    expect_error(lambda: transition(it, other, "complete", 1, "fixed"), 403, "FORBIDDEN")
    assert transition(it, owner, "complete", 1, "fixed")["status"] == "done"


def check_stale_transition_409():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = item(m, t["id"], status="in_progress", assignee_id=m["id"])
    call(update_item, it["id"], m["id"], 1, {"priority": "urgent"})
    expect_error(lambda: transition(it, m, "complete", 1, "fixed"), 409, "STALE_VERSION")


# ---- Task 8: list, search, views, overview --------------------------------

def ids(u, **filters) -> set:
    from app.listing import list_items

    return {r["id"] for r in call(list_items, u["id"], page_size=100, **filters)["items"]}


def check_list_never_shows_other_teams():
    t1, t2 = team(), team()
    a, b = user("member", t1["id"]), user("member", t2["id"])
    mine, theirs = item(a, t1["id"]), item(b, t2["id"])
    seen = ids(a, team_id=str(t1["id"])) | ids(a, team_id=str(t2["id"]))
    assert mine["id"] in seen and theirs["id"] not in seen


def check_status_and_priority_filters():
    t = team()
    m = user("member", t["id"])
    hit = item(m, t["id"], status="in_progress", priority="urgent", assignee_id=m["id"])
    item(m, t["id"], status="open", priority="low")
    item(m, t["id"], status="done", priority="urgent")
    got = ids(m, team_id=str(t["id"]), status=["open", "in_progress"], priority=["urgent"])
    assert got == {hit["id"]}, got


def check_assignee_me_and_unassigned():
    t = team()
    m = user("member", t["id"])
    mine = item(m, t["id"], assignee_id=m["id"])
    free = item(m, t["id"])
    assert ids(m, team_id=str(t["id"]), assignee="me") == {mine["id"]}
    assert ids(m, team_id=str(t["id"]), assignee="unassigned") == {free["id"]}


def check_pages_and_has_more():
    from app.listing import list_items

    t = team()
    m = user("member", t["id"])
    sql("""INSERT INTO work_items (team_id, title, type, priority, status, requires_approval, created_by)
           SELECT %s, 'Paged ' || g, 'task', 'medium', 'open', false, %s FROM generate_series(1, 60) g""",
        (t["id"], m["id"]))
    pages = [call(list_items, m["id"], team_id=str(t["id"]), page=p, page_size=25) for p in (1, 2, 3)]
    assert [len(p["items"]) for p in pages] == [25, 25, 10]
    assert [p["has_more"] for p in pages] == [True, True, False]
    all_ids = [r["id"] for p in pages for r in p["items"]]
    assert len(set(all_ids)) == 60


def check_search_stems():
    t = team()
    m = user("member", t["id"])
    hit = item(m, t["id"], title="Refund payment failed for merchant")
    item(m, t["id"], title="Rotate credentials")
    assert ids(m, team_id=str(t["id"]), q="payments") == {hit["id"]}


def check_search_garbage_ok():
    t = team()
    m = user("member", t["id"])
    item(m, t["id"], title="Something ordinary")
    for q in ['foo & (bar', '"', "   ", "!!! :* <-> |", "'; drop table users; --"]:
        ids(m, team_id=str(t["id"]), q=q)  # must not raise
    assert len(ids(m, team_id=str(t["id"]), q="   ")) == 1  # blank search is ignored


def check_stale_view():
    t = team()
    m = user("member", t["id"])
    old = item(m, t["id"])
    item(m, t["id"])
    sql("UPDATE work_items SET last_activity_at = now() - interval '4 days' WHERE id = %s", (old["id"],))
    assert ids(m, team_id=str(t["id"]), view="stale") == {old["id"]}


def check_awaiting_my_approval_excludes_own_and_non_lead_teams():
    t1, t2 = team(), team()
    lead = user("lead", t1["id"])
    add_member(lead["id"], t2["id"], "member")
    other = user("member", t1["id"])
    other2 = user("member", t2["id"])
    theirs = item(other, t1["id"], status="awaiting_approval", requires_approval=True)
    item(lead, t1["id"], status="awaiting_approval", requires_approval=True)  # own: excluded
    item(other2, t2["id"], status="awaiting_approval", requires_approval=True)  # not lead there: excluded
    assert ids(lead, view="awaiting_my_approval") == {theirs["id"]}


def check_overview_counts_match_lists():
    from app.listing import VIEWS, overview_counts

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    item(m, t["id"], assignee_id=lead["id"], status="in_progress")
    item(m, t["id"], priority="urgent")
    item(m, t["id"], status="awaiting_approval", requires_approval=True)
    overdue = item(m, t["id"])
    sql("UPDATE work_items SET due_at = now() - interval '1 day' WHERE id = %s", (overdue["id"],))
    counts = call(overview_counts, lead["id"])
    for view in VIEWS:
        assert counts[view] == len(ids(lead, view=view)), (view, counts[view])
    assert counts["mine"] == 1 and counts["awaiting_my_approval"] == 1 and counts["overdue"] == 1


def check_unknown_view_400():
    m = user()
    expect_error(lambda: ids(m, view="everything"), 400, "INVALID_VIEW")


# ---- Final review fixes ---------------------------------------------------

def asgi(app, method: str, path: str, body=None, headers=None, on_start=None) -> tuple[int, dict, bytes]:
    """Call the real FastAPI app in-process (no HTTP server, no extra deps).
    `on_start` runs at the moment the response starts being sent."""
    import asyncio
    import json as _json

    raw = _json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"content-type", b"application/json")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
             "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1), "root_path": ""}
    out = {"status": 0, "headers": {}, "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
            if on_start:
                on_start()
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["headers"], out["body"]


def check_change_is_committed_before_response_is_sent():
    from app.main import create_app

    app = create_app()
    app.state.pool = pool  # skip lifespan; reuse the check pool
    t = team()
    m = user("member", t["id"])
    _, h, _ = asgi(app, "POST", "/api/login", {"email": m["email"], "password": "password"})
    cookie = h["set-cookie"].split(";")[0]
    seen = {}
    status, _, _ = asgi(app, "POST", "/api/items",
                        {"team_id": str(t["id"]), "title": "Commit timing", "type": "task", "priority": "low"},
                        {"cookie": cookie, "idempotency-key": "commit-1"},
                        on_start=lambda: seen.setdefault("count", item_count(t["id"])))
    assert status == 201, status
    assert seen["count"] == 1, "the client was told 201 before the item was committed"


def check_stale_names_the_editor_not_a_later_commenter():
    from app.items import add_comment, update_item

    t = team()
    editor = user("member", t["id"], name="The Editor")
    commenter = user("member", t["id"], name="The Commenter")
    it = create(editor, t["id"])
    call(update_item, it["id"], editor["id"], 1, {"title": "Edited"})
    call(add_comment, it["id"], commenter["id"], "just a comment")  # doesn't change the item
    err = expect_error(lambda: call(update_item, it["id"], commenter["id"], 1, {"title": "stale"}), 409, "STALE_VERSION")
    assert err.details["changed_by"] == {"id": editor["id"], "name": "The Editor"}, err.details["changed_by"]


# ---- Cross-team requests, restriction, assignment, moving -----------------

def check_cross_team_request_requester_can_follow_and_edit():
    from app.items import claim_item, get_item, update_item

    owners = team()
    lead = user("lead", owners["id"])
    requester = user()  # in no team
    it = create(requester, owners["id"], title="Please refund order 77")
    assert it["my_role"] == "requester" and "claim" not in it["allowed_actions"], it["allowed_actions"]
    assert call(get_item, it["id"], lead["id"])["my_role"] == "lead"  # owning team sees it
    edited = call(update_item, it["id"], requester["id"], 1, {"title": "Please refund order 78"})
    assert edited["title"] == "Please refund order 78"
    expect_error(lambda: call(claim_item, it["id"], requester["id"]), 403, "FORBIDDEN")
    assert it["id"] in ids(requester, view="requested_by_me")
    assert it["id"] not in ids(lead, view="requested_by_me")


def check_restricted_item_details_locked_for_members_not_status():
    from app.items import update_item

    t = team()
    creator, worker, lead = user("member", t["id"]), user("member", t["id"]), user("lead", t["id"])
    it = create(creator, t["id"], restricted=True, assignee_id=creator["id"])
    sql("UPDATE work_items SET assignee_id = %s WHERE id = %s", (worker["id"], it["id"]))
    expect_error(lambda: call(update_item, it["id"], worker["id"], 1, {"title": "nope"}), 403, "FORBIDDEN")
    v = call(update_item, it["id"], creator["id"], 1, {"title": "creator edit"})["version"]
    v = call(update_item, it["id"], lead["id"], v, {"title": "lead edit"})["version"]
    assert transition(it, worker, "start", v)["status"] == "in_progress"  # status not locked


def check_only_leads_and_creator_change_restriction():
    from app.items import update_item

    t = team()
    creator, other = user("member", t["id"]), user("member", t["id"])
    it = create(creator, t["id"])
    expect_error(lambda: call(update_item, it["id"], other["id"], 1, {"restricted": True}), 403, "FORBIDDEN")
    assert call(update_item, it["id"], creator["id"], 1, {"restricted": True})["restricted"] is True


def check_assign_at_create_rules():
    t = team()
    lead, m, other = user("lead", t["id"]), user("member", t["id"]), user("member", t["id"])
    assert create(lead, t["id"], assignee_id=m["id"])["assignee"]["id"] == m["id"]
    got = create(m, t["id"], assignee_id=other["id"])  # any member may assign anyone in the team
    assert got["assignee"]["id"] == other["id"] and got["assigned_by"]["id"] == m["id"]
    expect_error(lambda: create(lead, t["id"], assignee_id=user()["id"]), 422, "INVALID_ASSIGNEE")
    expect_error(lambda: create(user(), t["id"], assignee_id=m["id"]), 403, "FORBIDDEN")  # requester


def check_assigner_can_reassign_but_assignee_cannot_pass_it_on():
    from app.items import assign_item, update_item

    t = team()
    assigner, worker, third = user("member", t["id"]), user("member", t["id"]), user("member", t["id"])
    it = create(assigner, t["id"], assignee_id=worker["id"])
    assert "assign" not in call(__import__("app.items", fromlist=["x"]).get_item, it["id"], worker["id"])["allowed_actions"]
    expect_error(lambda: call(assign_item, it["id"], worker["id"], third["id"], it["version"]), 403, "FORBIDDEN")
    expect_error(lambda: call(update_item, it["id"], worker["id"], it["version"], {"assignee_id": third["id"]}),
                 403, "FORBIDDEN")
    moved = call(assign_item, it["id"], assigner["id"], third["id"], it["version"])
    assert moved["assignee"]["id"] == third["id"] and moved["assigned_by"]["id"] == assigner["id"]


def check_leads_never_get_restrict():
    from app.items import update_item

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    expect_error(lambda: create(lead, t["id"], restricted=True), 403, "FORBIDDEN")
    it = create(lead, t["id"])
    assert "restrict" not in it["allowed_actions"]
    expect_error(lambda: call(update_item, it["id"], lead["id"], 1, {"restricted": True}), 403, "FORBIDDEN")
    assert "restrict" in create(m, t["id"])["allowed_actions"]  # a member creator does get it


def check_move_team_clears_foreign_assignee_and_logs():
    from app.items import update_item

    a, b = team(), team()
    lead = user("lead", a["id"])
    worker = user("member", a["id"])
    it = create(lead, a["id"], assignee_id=worker["id"])
    moved = call(update_item, it["id"], lead["id"], 1, {"team_id": b["id"]})
    assert moved["team"]["id"] == b["id"] and moved["assignee"] is None
    # lead created it, so keeps access as requester in the new team
    assert moved["my_role"] == "requester"
    payloads = [r["payload"] for r in sql(
        "SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (it["id"],))]
    assert payloads[-1]["changes"]["team"] == {"from": a["name"], "to": b["name"]}, payloads
    assert activity_kinds(it["id"])[-1] == "assigned"


def check_move_by_member_who_loses_access_returns_no_actions():
    from app.items import get_item, update_item

    a, b = team(), team()
    creator, mover = user("member", a["id"]), user("member", a["id"])
    it = create(creator, a["id"])
    moved = call(update_item, it["id"], mover["id"], 1, {"team_id": b["id"]})
    assert moved["allowed_actions"] == []
    expect_error(lambda: call(get_item, it["id"], mover["id"]), 404, "NOT_FOUND")


def check_list_rows_carry_allowed_actions_and_counts_match():
    from app.listing import status_counts

    t = team()
    m = user("member", t["id"])
    mine = item(m, t["id"], status="in_progress", assignee_id=m["id"])
    item(m, t["id"], status="open")
    item(m, t["id"], status="done")
    rows = {r["id"]: r for r in call(__import__("app.listing", fromlist=["x"]).list_items, m["id"],
                                      team_id=str(t["id"]))["items"]}
    assert "complete" in rows[mine["id"]]["allowed_actions"]
    counts = call(status_counts, m["id"], team_id=str(t["id"]))
    assert (counts["in_progress"], counts["open"], counts["done"], counts["blocked"]) == (1, 1, 1, 0), counts


def check_change_notifies_only_on_commit():
    import psycopg as _pg

    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"])
    with _pg.connect(DATABASE_URL, autocommit=True) as listener:
        listener.execute("LISTEN item_changes")
        list(listener.notifies(timeout=0.2))  # drain anything older
        try:  # a change that rolls back must not be announced
            with tx() as conn:
                update_item(conn, it["id"], m["id"], 1, {"title": "rolled back"})
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        assert [n for n in listener.notifies(timeout=0.5)] == []
        call(update_item, it["id"], m["id"], 1, {"title": "committed"})
        notes = [__import__("json").loads(n.payload) for n in listener.notifies(timeout=2, stop_after=1)]
    assert notes and notes[0]["item_id"] == str(it["id"]) and notes[0]["team_ids"] == [str(t["id"])], notes


def check_event_routing_respects_visibility():
    from app.events import Subscriber

    sub = Subscriber("u1", {"team-a"})
    assert sub.wants({"team_ids": ["team-a"], "created_by": "x"})
    assert sub.wants({"team_ids": ["team-z"], "created_by": "u1"})  # my request elsewhere
    assert not sub.wants({"team_ids": ["team-z"], "created_by": "x"})
    assert sub.wants({"team_ids": ["team-z", "team-a"], "created_by": "x"})  # moved out of my team


# ---- Sign-up --------------------------------------------------------------

def signup_data(**kw):
    from app.auth import SignupIn

    n = _next()
    return SignupIn(**{"name": f"New Person {n}", "email": f"{RUN}-{n}@check.test", "password": "longenough", **kw})


def role_of(user_id) -> list[tuple]:
    return [(r["name"], r["role"]) for r in sql(
        "SELECT t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id WHERE m.user_id = %s",
        (user_id,))]


def check_signup_new_team_makes_lead_and_logs_in():
    from app.main import create_app

    app = create_app()
    app.state.pool = pool
    n = _next()
    body = {"name": "Founder", "email": f"{RUN}-{n}@CHECK.test", "password": "longenough",
            "new_team_name": f"check-{RUN}-{n}"}
    status, h, raw = asgi(app, "POST", "/api/signup", body)
    assert status == 201, (status, raw)
    cookie = h["set-cookie"].split(";")[0]
    status, _, raw = asgi(app, "GET", "/api/me", headers={"cookie": cookie})
    me = __import__("json").loads(raw)
    assert status == 200 and me["user"]["email"] == f"{RUN}-{n}@check.test"  # stored lower-case
    assert [(t["name"], t["role"]) for t in me["teams"]] == [(f"check-{RUN}-{n}", "lead")]
    # and the new account can log in with its password
    status, _, _ = asgi(app, "POST", "/api/login", {"email": body["email"], "password": "longenough"})
    assert status == 200


def check_signup_join_existing_team_as_member():
    from app.auth import signup

    t = team()
    u = call(signup, signup_data(team_id=t["id"]))
    assert role_of(u["id"]) == [(t["name"], "member")]


def check_signup_duplicate_email_409_case_insensitive():
    from app.auth import signup

    t = team()
    first = signup_data(team_id=t["id"])
    call(signup, first)
    again = signup_data(team_id=t["id"], email=first.email.upper())
    expect_error(lambda: call(signup, again), 409, "EMAIL_TAKEN")


def check_signup_taken_team_name_409_creates_nothing():
    from app.auth import signup

    t = team()
    data = signup_data(new_team_name=t["name"])
    expect_error(lambda: call(signup, data), 409, "TEAM_NAME_TAKEN")
    assert sql("SELECT 1 FROM users WHERE email = %s", (data.email,)) == [], "user row must roll back"


def check_signup_needs_exactly_one_team_choice():
    from pydantic import ValidationError

    t = team()
    for kw in ({}, {"team_id": t["id"], "new_team_name": "x"}):
        try:
            signup_data(**kw)
            raise AssertionError(f"accepted {kw}")
        except ValidationError:
            pass
    try:
        signup_data(team_id=t["id"], password="short")
        raise AssertionError("accepted a 5-character password")
    except ValidationError:
        pass


def check_concurrent_signups_same_email_one_wins():
    from app.auth import signup

    t = team()
    data = signup_data(team_id=t["id"])
    with ThreadPoolExecutor(5) as ex:
        results = list(ex.map(lambda _: outcome(lambda: call(signup, data)), range(5)))
    assert sorted(c for c, _ in results) == ["EMAIL_TAKEN"] * 4 + ["ok"], [c for c, _ in results]
    assert len(sql("SELECT 1 FROM users WHERE email = %s", (data.email,))) == 1


# ---- runner ---------------------------------------------------------------

def main() -> int:
    checks = [(n, f) for n, f in globals().items() if n.startswith("check_") and callable(f)]
    failed = 0
    try:
        for name, fn in checks:
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # report and keep going
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                if "-v" in sys.argv:
                    traceback.print_exc()
    finally:
        cleanup()
        pool.close()
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
