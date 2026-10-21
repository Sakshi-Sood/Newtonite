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
    from app.statuses import create_defaults
    from app.task_types import create_defaults as create_default_types

    row = sql("INSERT INTO teams (name) VALUES (%s) RETURNING *", (f"check-{RUN}-{_next()}",))[0]
    with tx() as conn:
        create_defaults(conn, row["id"])  # To do / In progress / Completed / Unassigned
        create_default_types(conn, row["id"])  # Customer issue ... Task
    return row


def type_id(team_id, name: str = "Task"):
    return sql("SELECT id FROM task_types WHERE team_id = %s AND name = %s", (team_id, name))[0]["id"]


def status_id(team_id, kind: str = "todo"):
    return sql("SELECT id FROM statuses WHERE team_id = %s AND kind = %s", (team_id, kind))[0]["id"]


def custom_status(team_id, name: str, finished: bool = False) -> dict:
    return sql("""INSERT INTO statuses (team_id, name, kind, color, finished, position)
                  VALUES (%s, %s, 'custom', 'slate', %s,
                          (SELECT coalesce(max(position), 0) + 1 FROM statuses WHERE team_id = %s)) RETURNING *""",
               (team_id, name, finished, team_id))[0]


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


def item(created_by: dict, team_id, status: str = "todo", **overrides) -> dict:
    """Insert a task straight into the database. `status` is a default kind
    (todo / in_progress / completed); pass status_id=... for a custom status."""
    fields = {
        "team_id": team_id, "title": f"Check item {_next()}", "description": "",
        "type_id": type_id(team_id), "priority": "medium", "status_id": status_id(team_id, status),
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
    sql("DELETE FROM projects WHERE name LIKE %s", (f"check-{RUN}-%",))
    sql("DELETE FROM teams WHERE name LIKE %s", (f"check-{RUN}-%",))
    sql("DELETE FROM users WHERE email LIKE %s", (f"{RUN}-%@check.test",))


# ---- Task 1: schema -------------------------------------------------------

def check_schema():
    names = {r["table_name"] for r in sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    expected = {"users", "teams", "memberships", "statuses", "work_items", "activity", "idempotency_keys",
                "projects", "project_teams", "task_types"}
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

    data = ItemCreate(**{"team_id": team_id, "title": "Checked item", "type_id": type_id(team_id), "priority": "medium", **kw})
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
    assert it["status"]["name"] == "To do" and it["status"]["kind"] == "todo" and it["version"] == 1, it
    assert it["approval"] is None and not it["closed"]
    assert activity_kinds(it["id"]) == ["created"]


def check_requires_approval_starts_awaiting():
    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"], requires_approval=True)
    assert it["approval"] == "pending" and it["status"]["name"] == "To do", it


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
    assert "claim" in acts and "move" not in acts and "assign" not in acts, acts


# ---- Task 5: idempotency --------------------------------------------------

def idem_create(u: dict, team_id, key: str | None, title: str = "Idem item"):
    from app.idempotency import request_hash, run_idempotent
    from app.items import ItemCreate, create_item

    data = ItemCreate(team_id=team_id, title=title, type_id=type_id(team_id), priority="medium")
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


def check_patch_finished_item_403():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = item(m, t["id"], status="completed")
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


def move(it, u, to_status_id, version):
    from app.items import move_item

    return call(move_item, it["id"], u["id"], to_status_id, version)


def decide(it, u, decision, version, reason=None):
    from app.items import decide_approval

    return call(decide_approval, it["id"], u["id"], decision, reason, version)


def check_move_to_any_status_by_assignee_or_lead():
    t = team()
    owner, other, lead = user("member", t["id"]), user("member", t["id"]), user("lead", t["id"])
    it = item(owner, t["id"], assignee_id=owner["id"])
    out = move(it, owner, status_id(t["id"], "completed"), 1)  # To do -> Completed directly: any order
    assert out["status"]["kind"] == "completed" and out["version"] == 2 and out["closed"]
    expect_error(lambda: move(it, other, status_id(t["id"], "in_progress"), 2), 403, "FORBIDDEN")
    back = move(it, lead, status_id(t["id"], "in_progress"), 2)  # a lead reopens it
    assert back["status"]["kind"] == "in_progress" and not back["closed"]
    moves = [r["payload"]["to"] for r in sql(
        "SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'status_changed' ORDER BY id", (it["id"],))]
    assert moves == ["Completed", "In progress"], moves


def check_move_rejects_other_teams_status_and_ignores_same_status():
    a, b = team(), team()
    m = user("member", a["id"])
    it = item(m, a["id"], assignee_id=m["id"])
    expect_error(lambda: move(it, m, status_id(b["id"], "completed"), 1), 422, "INVALID_STATUS")
    same = move(it, m, status_id(a["id"], "todo"), 1)
    assert same["version"] == 1 and activity_kinds(it["id"]) == []


def check_stale_move_409():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    it = item(m, t["id"], status="in_progress", assignee_id=m["id"])
    call(update_item, it["id"], m["id"], 1, {"priority": "urgent"})
    expect_error(lambda: move(it, m, status_id(t["id"], "completed"), 1), 409, "STALE_VERSION")


def check_pending_approval_blocks_moves_until_another_lead_approves():
    t = team()
    lead1, lead2, m = user("lead", t["id"]), user("lead", t["id"]), user("member", t["id"])
    it = create(lead1, t["id"], requires_approval=True)
    assert it["approval"] == "pending" and "move" not in it["allowed_actions"]
    expect_error(lambda: move(it, lead1, status_id(t["id"], "in_progress"), 1), 422, "APPROVAL_REQUIRED")
    expect_error(lambda: decide(it, lead1, "approve", 1), 403, "FORBIDDEN")  # creator
    expect_error(lambda: decide(it, m, "approve", 1), 403, "FORBIDDEN")      # not a lead
    ok = decide(it, lead2, "approve", 1)
    assert ok["approval"] == "approved" and ok["version"] == 2
    assert move(it, lead1, status_id(t["id"], "in_progress"), 2)["status"]["kind"] == "in_progress"


def check_reject_closes_the_task_and_a_lead_can_still_approve_later():
    t = team()
    lead1, lead2 = user("lead", t["id"]), user("lead", t["id"])
    it = create(lead1, t["id"], requires_approval=True)
    out = decide(it, lead2, "reject", 1, "Out of budget")
    assert out["approval"] == "rejected" and out["closed"] and "move" not in out["allowed_actions"]
    payload = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'approval'", (it["id"],))[0]["payload"]
    assert payload == {"decision": "rejected", "reason": "Out of budget"}, payload
    expect_error(lambda: decide(it, lead2, "reject", 2), 422, "INVALID_APPROVAL_STATE")
    again = decide(it, lead2, "approve", 2)
    assert again["approval"] == "approved" and not again["closed"]


def check_approval_on_a_task_that_needs_none_422():
    t = team()
    lead1, lead2 = user("lead", t["id"]), user("lead", t["id"])
    it = create(lead1, t["id"])
    expect_error(lambda: decide(it, lead2, "approve", 1), 422, "INVALID_APPROVAL_STATE")


def check_finished_custom_status_closes_tasks_and_leaves_attention_views():
    from app.items import update_item

    t = team()
    m = user("member", t["id"])
    cancelled = custom_status(t["id"], "Cancelled", finished=True)
    it = item(m, t["id"], assignee_id=m["id"])
    sql("UPDATE work_items SET due_at = now() - interval '1 day' WHERE id = %s", (it["id"],))
    assert it["id"] in ids(m, view="mine") and it["id"] in ids(m, view="overdue")
    out = move(it, m, cancelled["id"], 1)
    assert out["closed"] and out["status"]["name"] == "Cancelled"
    assert it["id"] not in ids(m, view="mine") and it["id"] not in ids(m, view="overdue")
    expect_error(lambda: call(update_item, it["id"], m["id"], 2, {"title": "x"}), 403, "FORBIDDEN")


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
    item(m, t["id"], status="todo", priority="low")
    item(m, t["id"], status="completed", priority="urgent")
    got = ids(m, team_id=str(t["id"]), status=["to do", "In Progress"], priority=["urgent"])  # names, any case
    assert got == {hit["id"]}, got
    exact = ids(m, team_id=str(t["id"]), status_id=str(status_id(t["id"], "in_progress")))
    assert exact == {hit["id"]}, exact


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
    sql("""INSERT INTO work_items (team_id, title, type_id, priority, status_id, requires_approval, created_by)
           SELECT %s, 'Paged ' || g, %s, 'medium', %s, false, %s FROM generate_series(1, 60) g""",
        (t["id"], type_id(t["id"]), status_id(t["id"]), m["id"]))
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
    theirs = item(other, t1["id"], approval="pending", requires_approval=True)
    item(lead, t1["id"], approval="pending", requires_approval=True)  # own: excluded
    item(other2, t2["id"], approval="pending", requires_approval=True)  # not lead there: excluded
    item(other, t1["id"], approval="approved", requires_approval=True)  # already decided: excluded
    assert ids(lead, view="awaiting_my_approval") == {theirs["id"]}


def check_overview_counts_match_lists():
    from app.listing import VIEWS, overview_counts

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    item(m, t["id"], assignee_id=lead["id"], status="in_progress")
    item(m, t["id"], priority="urgent")
    item(m, t["id"], approval="pending", requires_approval=True)
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
                        {"team_id": str(t["id"]), "title": "Commit timing", "type_id": str(type_id(t["id"])), "priority": "low"},
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
    assert move(it, worker, status_id(t["id"], "in_progress"), v)["status"]["kind"] == "in_progress"  # not locked


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
    from app.listing import list_items, status_counts

    t = team()
    m = user("member", t["id"])
    mine = item(m, t["id"], status="in_progress", assignee_id=m["id"])
    item(m, t["id"])
    item(m, t["id"], status="completed")
    rows = {r["id"]: r for r in call(list_items, m["id"], team_id=str(t["id"]))["items"]}
    assert "move" in rows[mine["id"]]["allowed_actions"] and rows[mine["id"]]["status"]["name"] == "In progress"
    counts = call(status_counts, m["id"], team_id=str(t["id"]))
    assert counts == {str(status_id(t["id"], k)): 1 for k in ("todo", "in_progress", "completed")}, counts
    expect_error(lambda: call(status_counts, m["id"], team_id=None), 400, "TEAM_REQUIRED")


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


# ---- Statuses -------------------------------------------------------------

def check_every_team_gets_the_four_default_statuses():
    t = team()
    rows = sql("SELECT name, kind, finished FROM statuses WHERE team_id = %s ORDER BY position", (t["id"],))
    assert [(r["name"], r["kind"], r["finished"]) for r in rows] == [
        ("To do", "todo", False), ("In progress", "in_progress", False), ("Completed", "completed", True),
        ("Unassigned", "unassigned", False)]


def check_leads_add_custom_statuses_members_can_only_read():
    from app import statuses as st

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    s_ = call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="In review", color="teal"))
    assert s_["kind"] == "custom" and s_["position"] == 5 and not s_["finished"]
    expect_error(lambda: call(st.create_status, m["id"], t["id"], st.StatusCreate(name="Nope")), 403, "FORBIDDEN")
    expect_error(lambda: call(st.create_status, user()["id"], t["id"], st.StatusCreate(name="Nope")), 404, "NOT_FOUND")
    expect_error(lambda: call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="in REVIEW")),
                 409, "STATUS_EXISTS")
    names = [x["name"] for x in call(st.list_team, m["id"], t["id"])]
    assert names == ["To do", "In progress", "Completed", "Unassigned", "In review"], names


def check_default_statuses_cannot_be_renamed_or_deleted():
    from app import statuses as st

    t = team()
    lead = user("lead", t["id"])
    todo = status_id(t["id"], "todo")
    expect_error(lambda: call(st.update_status, lead["id"], t["id"], todo, st.StatusPatch(name="Backlog")),
                 403, "DEFAULT_STATUS")
    expect_error(lambda: call(st.update_status, lead["id"], t["id"], todo, st.StatusPatch(finished=True)),
                 403, "DEFAULT_STATUS")
    expect_error(lambda: call(st.delete_status, lead["id"], t["id"], todo, None), 403, "DEFAULT_STATUS")
    unassigned = status_id(t["id"], "unassigned")
    expect_error(lambda: call(st.update_status, lead["id"], t["id"], unassigned, st.StatusPatch(name="Limbo")),
                 403, "DEFAULT_STATUS")
    expect_error(lambda: call(st.delete_status, lead["id"], t["id"], unassigned, None), 403, "DEFAULT_STATUS")
    assert call(st.update_status, lead["id"], t["id"], todo, st.StatusPatch(color="red"))["color"] == "red"


def check_custom_statuses_rename_and_reorder():
    from app import statuses as st

    t = team()
    lead = user("lead", t["id"])
    a = call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="Alpha"))
    b = call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="Beta", finished=True))
    assert call(st.update_status, lead["id"], t["id"], a["id"], st.StatusPatch(name="Review"))["name"] == "Review"
    expect_error(lambda: call(st.update_status, lead["id"], t["id"], a["id"], st.StatusPatch(name="beta")),
                 409, "STATUS_EXISTS")
    order = [status_id(t["id"], "completed"), b["id"], a["id"], status_id(t["id"], "todo"),
             status_id(t["id"], "in_progress"), status_id(t["id"], "unassigned")]
    got = call(st.reorder, lead["id"], t["id"], order)
    assert [x["id"] for x in got] == order
    expect_error(lambda: call(st.reorder, lead["id"], t["id"], order[:-1]), 422, "INVALID_ORDER")


def check_deleting_a_status_moves_its_tasks_and_records_it():
    from app import statuses as st

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    review = call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="Review"))
    one, two = item(m, t["id"], status_id=review["id"]), item(m, t["id"], status_id=review["id"])
    err = expect_error(lambda: call(st.delete_status, lead["id"], t["id"], review["id"], None), 409, "STATUS_IN_USE")
    assert err.details["task_count"] == 2
    expect_error(lambda: call(st.delete_status, lead["id"], t["id"], review["id"], review["id"]), 422, "INVALID_STATUS")
    call(st.delete_status, lead["id"], t["id"], review["id"], status_id(t["id"], "in_progress"))
    for it in (one, two):
        row = sql("""SELECT s.name, w.version FROM work_items w JOIN statuses s ON s.id = w.status_id
                     WHERE w.id = %s""", (it["id"],))[0]
        assert (row["name"], row["version"]) == ("In progress", 2), row
        assert activity_kinds(it["id"]) == ["status_changed"]
    assert sql("SELECT 1 FROM statuses WHERE id = %s", (review["id"],)) == []
    empty = call(st.create_status, lead["id"], t["id"], st.StatusCreate(name="Empty"))
    call(st.delete_status, lead["id"], t["id"], empty["id"], None)  # nothing to move


def check_team_move_maps_status_by_default_kind_then_name():
    from app.items import update_item

    a, b = team(), team()
    lead = user("lead", a["id"])
    review_a, only_a = custom_status(a["id"], "Review"), custom_status(a["id"], "Only here")
    custom_status(b["id"], "review")
    in_review = item(lead, a["id"], status_id=review_a["id"])
    lonely = item(lead, a["id"], status_id=only_a["id"])
    working = item(lead, a["id"], status="in_progress")
    got = {k: call(update_item, i["id"], lead["id"], 1, {"team_id": b["id"]})["status"]["name"]
           for k, i in (("review", in_review), ("lonely", lonely), ("working", working))}
    assert got == {"review": "review", "lonely": "To do", "working": "In progress"}, got
    changes = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (lonely["id"],))[0]
    assert changes["payload"]["changes"]["status"] == {"from": "Only here", "to": "To do"}, changes


def check_moving_a_task_to_unassigned_removes_its_assignee():
    from app.items import move_item

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    owned = item(lead, t["id"], assignee_id=m["id"], assigned_by=lead["id"])
    out = call(move_item, owned["id"], lead["id"], status_id(t["id"], "unassigned"), 1)
    assert out["status"]["kind"] == "unassigned" and out["assignee"] is None and out["version"] == 2, out
    assert activity_kinds(owned["id"]) == ["status_changed", "assigned"], activity_kinds(owned["id"])
    entry = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'assigned'", (owned["id"],))[0]
    assert entry["payload"]["from"] == str(m["id"]) and entry["payload"]["to"] is None, entry
    nobody = item(lead, t["id"])  # nothing to remove: only the status changes
    call(move_item, nobody["id"], lead["id"], status_id(t["id"], "unassigned"), 1)
    assert activity_kinds(nobody["id"]) == ["status_changed"]
    mine = item(lead, t["id"], assignee_id=m["id"], assigned_by=lead["id"])  # other moves keep the assignee
    assert call(move_item, mine["id"], m["id"], status_id(t["id"], "in_progress"), 1)["assignee"]["id"] == m["id"]


def check_leaving_unassigned_can_assign_in_the_same_step():
    from app.items import move_item

    t = team()
    lead, m, other = user("lead", t["id"]), user("member", t["id"]), user()
    unassigned, doing = status_id(t["id"], "unassigned"), status_id(t["id"], "in_progress")
    it = item(lead, t["id"], status_id=unassigned)
    expect_error(lambda: call(move_item, it["id"], lead["id"], doing, 1, other["id"]), 422, "INVALID_ASSIGNEE")  # not in the team
    waiting = item(lead, t["id"])
    expect_error(lambda: call(move_item, waiting["id"], lead["id"], unassigned, 1, m["id"]), 422, "INVALID_ASSIGNEE")  # Unassigned has no owner
    out = call(move_item, it["id"], lead["id"], doing, 1, m["id"])
    assert out["status"]["name"] == "In progress" and out["assignee"]["id"] == m["id"] and out["version"] == 2, out
    assert out["assigned_by"]["id"] == lead["id"]
    assert activity_kinds(it["id"]) == ["status_changed", "assigned"], activity_kinds(it["id"])
    plain = item(lead, t["id"], status_id=unassigned)  # no assignee given: just moves
    assert call(move_item, plain["id"], lead["id"], doing, 1)["assignee"] is None
    mine = item(lead, t["id"], status="in_progress", assignee_id=m["id"], assigned_by=lead["id"])  # an assignee can't pass it on
    expect_error(lambda: call(move_item, mine["id"], m["id"], status_id(t["id"], "completed"), 1, lead["id"]), 403, "FORBIDDEN")


# ---- Profile --------------------------------------------------------------

def check_profile_lists_account_teams_members_and_work_summary():
    from app.profile import get_profile

    t = team()
    lead, m = user("lead", t["id"], name="Pat Lead"), user("member", t["id"], name="Sam Member")
    item(lead, t["id"], status="in_progress", assignee_id=m["id"])
    late = item(lead, t["id"], assignee_id=m["id"])
    sql("UPDATE work_items SET due_at = now() - interval '2 days' WHERE id = %s", (late["id"],))
    item(m, t["id"], status="completed", assignee_id=m["id"])
    p = call(get_profile, m["id"])
    assert p["user"]["email"] == m["email"]
    mine = p["teams"][0]
    assert (mine["role"], mine["member_count"], mine["total_tasks"], mine["open_tasks"]) == ("member", 2, 3, 2), mine
    assert [(x["name"], x["role"]) for x in mine["members"]] == [("Pat Lead", "lead"), ("Sam Member", "member")]
    assert dict(p["stats"]) == {"assigned": 2, "overdue": 1, "finished": 1, "created": 1}, p["stats"]


def check_profile_name_and_password_changes():
    from app.auth import verify_password
    from app.profile import PasswordChange, ProfilePatch, change_password, update_profile

    m = user()
    assert call(update_profile, m["id"], ProfilePatch(name="  New Name "))["user"]["name"] == "New Name"
    expect_error(lambda: call(change_password, m["id"], PasswordChange(current_password="wrong", new_password="longenough1")),
                 422, "WRONG_PASSWORD")
    expect_error(lambda: call(change_password, m["id"], PasswordChange(current_password="password", new_password="password")),
                 422, "SAME_PASSWORD")
    call(change_password, m["id"], PasswordChange(current_password="password", new_password="brand-new-pass"))
    stored = sql("SELECT password_hash FROM users WHERE id = %s", (m["id"],))[0]["password_hash"]
    assert verify_password("brand-new-pass", stored) and not verify_password("password", stored)


# ---- Admin ----------------------------------------------------------------

def make_admin(**kw) -> dict:
    u = user(**kw)
    sql("UPDATE users SET is_admin = true WHERE id = %s", (u["id"],))
    return {**u, "is_admin": True}


def as_actor(u: dict) -> dict:
    """The user dict the API hands to admin functions."""
    return {"id": u["id"], "is_admin": True}


def api_app():
    from app.main import create_app

    app = create_app()
    app.state.pool = pool  # skip lifespan; reuse the check pool
    return app


def login_cookie(app, u: dict, password: str = "password") -> str:
    import json as _json

    status, h, raw = asgi(app, "POST", "/api/login", {"email": u["email"], "password": password})
    assert status == 200, (status, raw)
    return h["set-cookie"].split(";")[0]


def call_api(app, method: str, path: str, cookie: str | None = None, body=None):
    import json as _json

    status, h, raw = asgi(app, method, path, body, {"cookie": cookie} if cookie else None)
    return status, (_json.loads(raw) if raw else None)


@contextmanager
def scratch_db():
    """A throwaway schema with the real table definitions and NO rows, for checks that
    need an empty database. Its search_path excludes `public`, so it can't see real data."""
    import psycopg
    from psycopg.rows import dict_row

    from app.db import apply_schema

    name = f"check_scratch_{RUN}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        c.execute(f'CREATE SCHEMA "{name}"')
    try:
        with psycopg.connect(DATABASE_URL, options=f"-csearch_path={name}", row_factory=dict_row) as conn:
            assert conn.execute("SELECT to_regclass('users') IS NULL AS empty").fetchone()["empty"], "scratch sees real tables"
            apply_schema(conn)
            conn.commit()
            yield conn
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def raw_user(conn, name: str, is_admin=False, active=True) -> dict:
    from app.auth import hash_password

    return conn.execute("""INSERT INTO users (email, name, password_hash, is_admin, active)
                           VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                        (f"{name.lower()}@scratch.test", name, hash_password("password"), is_admin, active)).fetchone()


def check_first_admin_setup_only_on_an_empty_database():
    from app.auth import SetupIn, create_first_admin, setup_needed

    with tx() as conn:  # the real database has users
        assert setup_needed(conn) is False
    expect_error(lambda: call(create_first_admin, SetupIn(name="X", email="x@y.test", password="longenough")),
                 409, "SETUP_DONE")
    with scratch_db() as conn:
        assert setup_needed(conn) is True
        first = create_first_admin(conn, SetupIn(name="Ada", email="ADA@Example.test", password="longenough"))
        assert first["is_admin"] and first["email"] == "ada@example.test" and not first["must_change_password"]
        conn.commit()
        assert setup_needed(conn) is False
        expect_error(lambda: create_first_admin(conn, SetupIn(name="Eve", email="eve@example.test", password="longenough")),
                     409, "SETUP_DONE")


def check_admin_routes_need_an_admin_and_signup_is_gone():
    app = api_app()
    t = team()
    admin_u, member_u = make_admin(), user("member", t["id"])
    assert call_api(app, "GET", "/api/admin/summary")[0] == 401
    assert call_api(app, "GET", "/api/teams")[0] == 401  # team names are no longer public
    cookie = login_cookie(app, member_u)
    for method, path in (("GET", "/api/admin/summary"), ("GET", "/api/admin/teams"), ("GET", "/api/admin/users"),
                         ("POST", "/api/admin/teams"), ("POST", "/api/admin/users"),
                         ("GET", "/api/admin/projects"), ("POST", "/api/admin/projects")):
        status, body = call_api(app, method, path, cookie, {} if method == "POST" else None)
        assert status == 403 and body["error"]["code"] == "FORBIDDEN", (method, path, status, body)
    assert call_api(app, "GET", "/api/admin/summary", login_cookie(app, admin_u))[0] == 200
    assert call_api(app, "POST", "/api/signup", None, {"name": "x", "email": "x@y.test", "password": "longenough",
                                                         "new_team_name": "t"})[0] in (404, 405)


def check_temporary_password_must_be_changed_before_anything_else():
    app = api_app()
    u = user()
    sql("UPDATE users SET must_change_password = true WHERE id = %s", (u["id"],))
    status, body = call_api(app, "POST", "/api/login", None, {"email": u["email"], "password": "password"})
    assert status == 200 and body["user"]["must_change_password"] is True
    cookie = login_cookie(app, u)
    status, body = call_api(app, "GET", "/api/items", cookie)
    assert (status, body["error"]["code"]) == (403, "PASSWORD_CHANGE_REQUIRED"), (status, body)
    assert call_api(app, "GET", "/api/me", cookie)[0] == 200  # the page that asks for the new password still loads
    assert call_api(app, "GET", "/api/admin/summary", cookie)[0] == 403
    status, _ = call_api(app, "POST", "/api/profile/password", cookie, {"current_password": "password", "new_password": "my-own-pass1"})
    assert status == 204
    assert call_api(app, "GET", "/api/items", cookie)[0] == 200
    assert login_cookie(app, u, "my-own-pass1")


def check_deactivated_user_is_locked_out_at_once():
    from app.admin import UserPatch, update_user

    app = api_app()
    admin_u, victim = make_admin(), user()
    cookie = login_cookie(app, victim)
    assert call_api(app, "GET", "/api/me", cookie)[0] == 200
    call(update_user, as_actor(admin_u), victim["id"], UserPatch(active=False))
    assert call_api(app, "GET", "/api/me", cookie)[0] == 401  # the old session stops working immediately
    status, body = call_api(app, "POST", "/api/login", None, {"email": victim["email"], "password": "password"})
    assert (status, body["error"]["code"]) == (403, "ACCOUNT_DISABLED")
    status, body = call_api(app, "POST", "/api/login", None, {"email": victim["email"], "password": "wrong"})
    assert (status, body["error"]["code"]) == (401, "INVALID_CREDENTIALS")  # no hint to someone without the password
    call(update_user, as_actor(admin_u), victim["id"], UserPatch(active=True))
    assert login_cookie(app, victim)


def check_admin_creates_a_user_in_several_teams_atomically():
    from app.admin import MembershipIn, UserCreate, create_user
    from app.auth import verify_password

    t1, t2, empty = team(), team(), team()
    user("lead", t1["id"])
    user("lead", t2["id"])
    made = call(create_user, UserCreate(name="New Hire", email=f"{RUN}-New.Hire@Check.test", temporary_password="temp-pass-1",
                                        memberships=[MembershipIn(team_id=t1["id"], role="member"),
                                                     MembershipIn(team_id=t2["id"], role="lead")]))
    assert made["email"] == f"{RUN}-new.hire@check.test" and made["must_change_password"] and not made["is_admin"]
    assert sorted((x["role"]) for x in made["teams"]) == ["lead", "member"]
    row = sql("SELECT password_hash FROM users WHERE id = %s", (made["id"],))[0]
    assert verify_password("temp-pass-1", row["password_hash"]) and "temp-pass-1" not in row["password_hash"]
    expect_error(lambda: call(create_user, UserCreate(name="Dup", email=f"{RUN}-new.hire@check.test", temporary_password="temp-pass-1")),
                 409, "EMAIL_TAKEN")
    # a leadless team can't get a plain member: nothing at all is created
    expect_error(lambda: call(create_user, UserCreate(name="Orphan", email=f"{RUN}-orphan@check.test", temporary_password="temp-pass-1",
                                                      memberships=[MembershipIn(team_id=empty["id"], role="member")])),
                 409, "TEAM_NEEDS_LEAD")
    assert sql("SELECT 1 FROM users WHERE email = %s", (f"{RUN}-orphan@check.test",)) == []
    lead_first = call(create_user, UserCreate(name="First Lead", email=f"{RUN}-first@check.test", temporary_password="temp-pass-1",
                                              memberships=[MembershipIn(team_id=empty["id"], role="lead")]))
    assert lead_first["teams"][0]["role"] == "lead"
    expect_error(lambda: call(create_user, UserCreate(name="Z", email=f"{RUN}-z@check.test", temporary_password="temp-pass-1",
                                                      memberships=[MembershipIn(team_id=uuid.uuid4(), role="lead")])),
                 422, "INVALID_TEAM")
    expect_error(lambda: call(create_user, UserCreate(name="D", email=f"{RUN}-d@check.test", temporary_password="temp-pass-1",
                                                      memberships=[MembershipIn(team_id=t1["id"]), MembershipIn(team_id=t1["id"])])),
                 422, "DUPLICATE_TEAM")


def check_team_lifecycle_create_rename_delete():
    from app.admin import TeamDelete, TeamIn, create_team, delete_team, rename_team

    name = f"check-{RUN}-lifecycle"
    t = call(create_team, TeamIn(name=name))
    kinds = [r["kind"] for r in sql("SELECT kind FROM statuses WHERE team_id = %s ORDER BY position", (t["id"],))]
    assert kinds == ["todo", "in_progress", "completed", "unassigned"], kinds
    assert [r["name"] for r in sql("SELECT name FROM task_types WHERE team_id = %s ORDER BY position", (t["id"],))] == [
        "Customer issue", "Engineering", "Payment", "Incident", "Compliance", "Task"]
    expect_error(lambda: call(create_team, TeamIn(name=name)), 409, "TEAM_NAME_TAKEN")
    other = call(create_team, TeamIn(name=f"{name}-other"))
    expect_error(lambda: call(rename_team, other["id"], TeamIn(name=name)), 409, "TEAM_NAME_TAKEN")
    assert call(rename_team, other["id"], TeamIn(name=f"{name}-renamed"))["name"] == f"{name}-renamed"
    expect_error(lambda: call(rename_team, uuid.uuid4(), TeamIn(name="nope")), 404, "NOT_FOUND")
    lead = user("lead", t["id"])
    item(lead, t["id"])
    expect_error(lambda: call(delete_team, t["id"], TeamDelete(confirm_name="wrong")), 422, "CONFIRM_MISMATCH")
    assert sql("SELECT 1 FROM teams WHERE id = %s", (t["id"],))
    call(delete_team, t["id"], TeamDelete(confirm_name=f"  {name} "))
    for table in ("teams", "statuses", "memberships", "work_items"):
        col = "id" if table == "teams" else "team_id"
        assert sql(f"SELECT 1 FROM {table} WHERE {col} = %s", (t["id"],)) == [], table
    assert sql("SELECT 1 FROM users WHERE id = %s", (lead["id"],)), "deleting a team must not delete its people"


def check_a_team_with_members_always_keeps_a_lead():
    from app.admin import remove_membership, set_membership

    t = team()
    admin_u = make_admin()
    lead, member = user("lead", t["id"]), user("member", t["id"])
    expect_error(lambda: call(set_membership, lead["id"], t["id"], "member"), 409, "TEAM_NEEDS_LEAD")
    expect_error(lambda: call(remove_membership, as_actor(admin_u), lead["id"], t["id"]), 409, "TEAM_NEEDS_LEAD")
    assert sql("SELECT role FROM memberships WHERE user_id = %s", (lead["id"],))[0]["role"] == "lead"  # unchanged
    call(set_membership, member["id"], t["id"], "lead")  # promote first ...
    call(set_membership, lead["id"], t["id"], "member")  # ... then the old lead may step down
    expect_error(lambda: call(remove_membership, as_actor(admin_u), member["id"], t["id"]), 409, "TEAM_NEEDS_LEAD")
    call(remove_membership, as_actor(admin_u), lead["id"], t["id"])  # a plain member can leave
    out = call(remove_membership, as_actor(admin_u), member["id"], t["id"])  # the last person can leave: team is empty
    assert out["teams"] == [] and sql("SELECT 1 FROM memberships WHERE team_id = %s", (t["id"],)) == []


def check_removing_a_member_unassigns_their_tasks_with_history():
    from app.admin import remove_membership

    t = team()
    admin_u = make_admin()
    lead, worker = user("lead", t["id"]), user("member", t["id"], name="Wanda Worker")
    mine = item(lead, t["id"], assignee_id=worker["id"])
    other = item(lead, t["id"], assignee_id=lead["id"])
    out = call(remove_membership, as_actor(admin_u), worker["id"], t["id"])
    assert out["teams"] == []
    row = sql("SELECT assignee_id, assigned_by, version FROM work_items WHERE id = %s", (mine["id"],))[0]
    assert row["assignee_id"] is None and row["assigned_by"] is None and row["version"] == 2, row
    entry = sql("SELECT actor_id, payload FROM activity WHERE work_item_id = %s AND kind = 'assigned'", (mine["id"],))[0]
    assert entry["actor_id"] == admin_u["id"] and entry["payload"]["from_name"] == "Wanda Worker" and entry["payload"]["to"] is None
    assert sql("SELECT assignee_id FROM work_items WHERE id = %s", (other["id"],))[0]["assignee_id"] == lead["id"]  # others untouched
    expect_error(lambda: call(remove_membership, as_actor(admin_u), worker["id"], t["id"]), 404, "NOT_FOUND")


def check_last_active_admin_and_self_deactivation_are_protected():
    from app.admin import UserPatch, update_user

    with scratch_db() as conn:
        a1, a2 = raw_user(conn, "Alpha", is_admin=True), raw_user(conn, "Beta", is_admin=True)
        gone = raw_user(conn, "Gamma", is_admin=True, active=False)
        conn.commit()
        act = {"id": a1["id"], "is_admin": True}
        expect_error(lambda: update_user(conn, act, a1["id"], UserPatch(active=False)), 409, "SELF_DEACTIVATE")
        assert update_user(conn, act, a2["id"], UserPatch(is_admin=False))["is_admin"] is False  # Alpha remains
        for patch in (UserPatch(is_admin=False), UserPatch(active=False)):
            expect_error(lambda: update_user(conn, {"id": a2["id"], "is_admin": True}, a1["id"], patch), 409, "LAST_ADMIN")
        # a deactivated admin doesn't count as one
        expect_error(lambda: update_user(conn, {"id": a2["id"], "is_admin": True}, a1["id"], UserPatch(is_admin=False)),
                     409, "LAST_ADMIN")
        assert update_user(conn, act, a2["id"], UserPatch(is_admin=True))["is_admin"] is True
        assert update_user(conn, act, a1["id"], UserPatch(is_admin=False))["is_admin"] is False  # Beta remains
        assert gone["active"] is False
        expect_error(lambda: update_user(conn, {"id": a2["id"], "is_admin": True}, a2["id"], UserPatch(is_admin=False)),
                     409, "LAST_ADMIN")


def check_admin_can_edit_users_and_reset_passwords():
    from app.admin import PasswordReset, UserPatch, reset_password, update_user
    from app.auth import verify_password

    admin_u, u, other = make_admin(), user(), user()
    out = call(update_user, as_actor(admin_u), u["id"], UserPatch(name="  Renamed  ", email=f"{RUN}-Renamed@Check.test"))
    assert out["name"] == "Renamed" and out["email"] == f"{RUN}-renamed@check.test"
    expect_error(lambda: call(update_user, as_actor(admin_u), u["id"], UserPatch(email=other["email"])), 409, "EMAIL_TAKEN")
    expect_error(lambda: call(update_user, as_actor(admin_u), uuid.uuid4(), UserPatch(name="x")), 404, "NOT_FOUND")
    assert call(update_user, as_actor(admin_u), u["id"], UserPatch(is_admin=True))["is_admin"] is True
    call(reset_password, u["id"], PasswordReset(temporary_password="reset-me-123"))
    row = sql("SELECT password_hash, must_change_password FROM users WHERE id = %s", (u["id"],))[0]
    assert row["must_change_password"] and verify_password("reset-me-123", row["password_hash"])


def check_deactivated_users_cannot_be_assigned_or_listed():
    from app.items import assign_item

    t = team()
    app = api_app()
    lead, gone = user("lead", t["id"]), user("member", t["id"])
    it = create(lead, t["id"])
    sql("UPDATE users SET active = false WHERE id = %s", (gone["id"],))
    expect_error(lambda: call(assign_item, it["id"], lead["id"], gone["id"], 1), 422, "INVALID_ASSIGNEE")
    status, members = call_api(app, "GET", f"/api/teams/{t['id']}/members", login_cookie(app, lead))
    assert status == 200 and [m["id"] for m in members] == [str(lead["id"])], members


def check_admin_lists_search_and_paginate():
    from app.admin import list_teams, list_users

    t1, t2, t3 = team(), team(), team()
    lead = user("lead", t1["id"], name="Zed Lead")
    add_member(lead["id"], t2["id"], "lead")
    users = [user("member", t1["id"], name=f"Pg{RUN} {i}") for i in range(5)]
    item(lead, t1["id"])
    item(lead, t1["id"], status="completed")
    page1 = call(list_users, f"pg{RUN}", 1, 3)
    page2 = call(list_users, f"PG{RUN}", 2, 3)
    assert (len(page1["items"]), page1["has_more"], len(page2["items"]), page2["has_more"]) == (3, True, 2, False), (page1, page2)
    zed = call(list_users, "zed lead", 1, 10)["items"]
    assert [x["name"] for x in zed] == ["Zed Lead"] and sorted(x["role"] for x in zed[0]["teams"]) == ["lead", "lead"]
    assert call(list_users, "no-such-person-xyz", 1, 10)["items"] == []
    teams = {x["name"]: x for x in call(list_teams, f"check-{RUN}", 1, 50)["items"]}
    assert (teams[t1["name"]]["member_count"], teams[t1["name"]]["lead_count"], teams[t1["name"]]["lead_names"],
            teams[t1["name"]]["total_tasks"], teams[t1["name"]]["open_tasks"]) == (6, 1, "Zed Lead", 2, 1), teams[t1["name"]]
    assert teams[t3["name"]]["member_count"] == 0 and teams[t3["name"]]["lead_names"] == ""
    assert call(list_teams, "%_", 1, 50)["items"] == []  # search text is not a wildcard pattern


# ---- Projects -------------------------------------------------------------

def project(*teams: dict) -> dict:
    """A project assigned to the given teams (inserted straight into the database)."""
    row = sql("INSERT INTO projects (name) VALUES (%s) RETURNING *", (f"check-{RUN}-p{_next()}",))[0]
    for t in teams:
        sql("INSERT INTO project_teams (project_id, team_id) VALUES (%s, %s)", (row["id"], t["id"]))
    return row


def task_state(task_id) -> dict:
    return sql("""SELECT w.project_id, w.version, s.kind AS status_kind FROM work_items w
                  JOIN statuses s ON s.id = w.status_id WHERE w.id = %s""", (task_id,))[0]


def check_project_admin_api_lifecycle_and_admin_only():
    app = api_app()
    t = team()
    admin_u, member_u = make_admin(), user("member", t["id"])
    member, admin_c = login_cookie(app, member_u), login_cookie(app, admin_u)
    name = f"check-{RUN}-api"
    assert call_api(app, "GET", "/api/admin/projects")[0] == 401
    for method, path, body in (("GET", "/api/admin/projects", None), ("POST", "/api/admin/projects", {"name": name}),
                               ("PATCH", f"/api/admin/projects/{uuid.uuid4()}", {"name": "x"}),
                               ("DELETE", f"/api/admin/projects/{uuid.uuid4()}", {"confirm_name": "x"}),
                               ("GET", f"/api/admin/projects/{uuid.uuid4()}/teams", None),
                               ("PUT", f"/api/admin/projects/{uuid.uuid4()}/teams/{t['id']}", None),
                               ("DELETE", f"/api/admin/projects/{uuid.uuid4()}/teams/{t['id']}", None)):
        status, out = call_api(app, method, path, member, body)
        assert status == 403 and out["error"]["code"] == "FORBIDDEN", (method, path, status, out)
    status, p = call_api(app, "POST", "/api/admin/projects", admin_c, {"name": f"  {name} ", "description": "Launch"})
    assert status == 201 and p["name"] == name and p["team_count"] == 0, (status, p)
    assert call_api(app, "POST", "/api/admin/projects", admin_c, {"name": name.upper()})[1]["error"]["code"] == "PROJECT_NAME_TAKEN"
    assert call_api(app, "POST", "/api/admin/projects", admin_c, {"name": "  "})[0] == 400
    assert call_api(app, "PATCH", f"/api/admin/projects/{p['id']}", admin_c, {"description": "New"})[1]["description"] == "New"
    assert call_api(app, "PATCH", f"/api/admin/projects/{p['id']}", admin_c, {})[0] == 400
    assert call_api(app, "PUT", f"/api/admin/projects/{p['id']}/teams/{t['id']}", admin_c)[0] == 200
    assert call_api(app, "PUT", f"/api/admin/projects/{p['id']}/teams/{t['id']}", admin_c)[0] == 200  # repeating is harmless
    from app.projects import list_projects

    assert call_api(app, "GET", "/api/admin/projects", admin_c)[0] == 200
    listed = call(list_projects, RUN, 1, 50)["items"]
    assert [(x["name"], x["team_count"], [y["name"] for y in x["teams"]]) for x in listed] == [(name, 1, [t["name"]])], listed
    mine = {x["id"]: x for x in call_api(app, "GET", f"/api/admin/projects/{p['id']}/teams", admin_c)[1]}
    assert mine[str(t["id"])]["assigned"] is True
    assert call_api(app, "DELETE", f"/api/admin/projects/{p['id']}/teams/{t['id']}", admin_c)[1]["released"] == 0
    assert call_api(app, "GET", "/api/admin/projects/not-an-id/teams", admin_c)[0] == 404
    assert call_api(app, "DELETE", f"/api/admin/projects/{p['id']}", admin_c, {"confirm_name": "wrong"})[0] == 422
    assert call_api(app, "DELETE", f"/api/admin/projects/{p['id']}", admin_c, {"confirm_name": name})[0] == 204
    assert sql("SELECT 1 FROM projects WHERE id = %s", (p["id"],)) == []


def check_project_names_are_unique_ignoring_case():
    from app.projects import ProjectIn, ProjectPatch, create_project, update_project

    one = call(create_project, ProjectIn(name=f"check-{RUN}-Alpha"))
    two = call(create_project, ProjectIn(name=f"check-{RUN}-Beta"))
    expect_error(lambda: call(create_project, ProjectIn(name=f"CHECK-{RUN}-alpha")), 409, "PROJECT_NAME_TAKEN")
    expect_error(lambda: call(update_project, two["id"], ProjectPatch(name=f"check-{RUN}-ALPHA")), 409, "PROJECT_NAME_TAKEN")
    assert call(update_project, one["id"], ProjectPatch(name=f"check-{RUN}-ALPHA"))["name"] == f"check-{RUN}-ALPHA"
    expect_error(lambda: call(update_project, uuid.uuid4(), ProjectPatch(name="x")), 404, "NOT_FOUND")


def check_a_task_can_only_carry_a_project_of_its_own_team():
    from app.items import update_item

    a, b = team(), team()
    m = user("member", a["id"])
    mine, theirs = project(a), project(b)
    expect_error(lambda: create(m, a["id"], project_id=theirs["id"]), 422, "INVALID_PROJECT")
    expect_error(lambda: create(m, a["id"], project_id=uuid.uuid4()), 422, "INVALID_PROJECT")
    it = create(m, a["id"], project_id=mine["id"])
    assert it["project"] == {"id": mine["id"], "name": mine["name"]}, it["project"]
    plain = create(m, a["id"])
    assert plain["project"] is None
    expect_error(lambda: call(update_item, plain["id"], m["id"], 1, {"project_id": theirs["id"]}), 422, "INVALID_PROJECT")
    tagged = call(update_item, plain["id"], m["id"], 1, {"project_id": mine["id"]})  # the creator tags it later
    assert tagged["project"]["name"] == mine["name"] and tagged["version"] == 2
    entry = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (plain["id"],))[0]
    assert entry["payload"]["changes"]["project"] == {"from": None, "to": mine["name"]}, entry
    cleared = call(update_item, plain["id"], m["id"], 2, {"project_id": None})
    assert cleared["project"] is None and cleared["version"] == 3
    assert call(update_item, plain["id"], m["id"], 3, {"project_id": None})["version"] == 3  # nothing changed


def check_taking_a_team_off_a_project_releases_its_tasks_to_unassigned():
    from app.projects import remove_team

    a, b = team(), team()
    admin_u = make_admin()
    lead_a, lead_b = user("lead", a["id"]), user("lead", b["id"])
    p = project(a, b)
    todo = item(lead_a, a["id"], project_id=p["id"])
    doing = item(lead_a, a["id"], status="in_progress", project_id=p["id"])
    done = item(lead_a, a["id"], status="completed", project_id=p["id"])
    waiting = item(lead_a, a["id"], project_id=p["id"], requires_approval=True, approval="pending")
    rejected = item(lead_a, a["id"], project_id=p["id"], requires_approval=True, approval="rejected")
    other_team = item(lead_b, b["id"], project_id=p["id"])
    untagged = item(lead_a, a["id"])
    out = call(remove_team, admin_u["id"], p["id"], a["id"])
    assert out["released"] == 5 and out["assigned"] is False, out
    for moved in (todo, doing):
        got = task_state(moved["id"])
        assert (got["project_id"], got["status_kind"], got["version"]) == (None, "unassigned", 2), got
        assert sorted(activity_kinds(moved["id"])) == ["status_changed", "updated"], activity_kinds(moved["id"])
    log = sql("SELECT actor_id, payload FROM activity WHERE work_item_id = %s AND kind = 'status_changed'", (doing["id"],))[0]
    assert log["actor_id"] == admin_u["id"] and log["payload"]["from"] == "In progress" \
        and log["payload"]["to"] == "Unassigned" and log["payload"]["reason"] == "Project changed", log
    change = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (todo["id"],))[0]
    assert change["payload"]["changes"]["project"] == {"from": p["name"], "to": None}, change
    for stays, kind in ((done, "completed"), (waiting, "todo"), (rejected, "todo")):  # untagged, status left alone
        got = task_state(stays["id"])
        assert (got["project_id"], got["status_kind"], got["version"]) == (None, kind, 2), (kind, got)
        assert activity_kinds(stays["id"]) == ["updated"]
    assert task_state(other_team["id"])["project_id"] == p["id"] and task_state(other_team["id"])["version"] == 1
    assert task_state(untagged["id"])["version"] == 1
    assert sql("SELECT count(*) AS n FROM work_items WHERE team_id = %s", (a["id"],))[0]["n"] == 6  # nothing deleted
    assert [r["team_id"] for r in sql("SELECT team_id FROM project_teams WHERE project_id = %s", (p["id"],))] == [b["id"]]
    assert call(remove_team, admin_u["id"], p["id"], a["id"])["released"] == 0  # repeating releases nothing


def check_deleting_a_project_releases_every_teams_tasks():
    from app.projects import ProjectDelete, delete_project

    a, b = team(), team()
    admin_u = make_admin()
    la, lb = user("lead", a["id"]), user("lead", b["id"])
    p = project(a, b)
    one, two = item(la, a["id"], project_id=p["id"]), item(lb, b["id"], status="completed", project_id=p["id"])
    expect_error(lambda: call(delete_project, admin_u["id"], p["id"], ProjectDelete(confirm_name="nope")), 422, "CONFIRM_MISMATCH")
    assert sql("SELECT 1 FROM projects WHERE id = %s", (p["id"],))
    call(delete_project, admin_u["id"], p["id"], ProjectDelete(confirm_name=f" {p['name']}"))
    assert sql("SELECT 1 FROM projects WHERE id = %s", (p["id"],)) == []
    assert sql("SELECT 1 FROM project_teams WHERE project_id = %s", (p["id"],)) == []
    assert (task_state(one["id"])["project_id"], task_state(one["id"])["status_kind"]) == (None, "unassigned")
    assert (task_state(two["id"])["project_id"], task_state(two["id"])["status_kind"]) == (None, "completed")
    assert sorted(activity_kinds(one["id"])) == ["status_changed", "updated"] and activity_kinds(two["id"]) == ["updated"]


def check_moving_a_task_drops_a_project_the_new_team_is_not_on():
    from app.items import update_item

    a, b, c = team(), team(), team()
    lead = user("lead", a["id"])
    p = project(a, b)
    keeps, loses = item(lead, a["id"], project_id=p["id"]), item(lead, a["id"], project_id=p["id"])
    assert call(update_item, keeps["id"], lead["id"], 1, {"team_id": b["id"]})["project"]["name"] == p["name"]
    moved = call(update_item, loses["id"], lead["id"], 1, {"team_id": c["id"]})
    assert moved["project"] is None
    entry = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (loses["id"],))[0]
    assert entry["payload"]["changes"]["project"] == {"from": p["name"], "to": None}, entry
    plain = item(lead, a["id"])  # asking for a project the destination team isn't on is refused
    expect_error(lambda: call(update_item, plain["id"], lead["id"], 1, {"team_id": c["id"], "project_id": p["id"]}),
                 422, "INVALID_PROJECT")


def check_project_filter_on_lists_and_board_counts():
    from app.listing import list_items, status_counts

    t = team()
    lead = user("lead", t["id"])
    p, q = project(t), project(t)
    in_p = [item(lead, t["id"], project_id=p["id"]), item(lead, t["id"], status="completed", project_id=p["id"])]
    in_q = item(lead, t["id"], project_id=q["id"])
    none_ = item(lead, t["id"])

    def ids(**kw):
        return {i["id"] for i in call(list_items, lead["id"], team_id=str(t["id"]), **kw)["items"]}

    assert ids(project=str(p["id"])) == {i["id"] for i in in_p}
    assert ids(project=str(q["id"])) == {in_q["id"]}
    assert ids(project="none") == {none_["id"]}
    assert len(ids()) == 4
    got = call(list_items, lead["id"], team_id=str(t["id"]), project=str(q["id"]))["items"][0]
    assert got["project"] == {"id": q["id"], "name": q["name"]}
    counts = call(status_counts, lead["id"], team_id=str(t["id"]), project=str(p["id"]))
    assert counts == {str(status_id(t["id"], "todo")): 1, str(status_id(t["id"], "completed")): 1}, counts
    assert call(status_counts, lead["id"], team_id=str(t["id"]), project="none") == {str(status_id(t["id"], "todo")): 1}
    expect_error(lambda: call(list_items, lead["id"], project="not-an-id"), 400, "INVALID_FILTER")
    outsider = user()  # filtering by a project never widens what a person may see
    assert call(list_items, outsider["id"], project=str(p["id"]))["items"] == []


def check_people_only_see_projects_of_their_own_teams_or_a_team_they_send_work_to():
    from app.projects import for_user, list_visible

    a, b = team(), team()
    m = user("member", a["id"])
    pa, pb = project(a), project(b)
    mine = [x["id"] for x in call(list_visible, m["id"], None)]
    assert pa["id"] in mine and pb["id"] not in mine
    assert [x["id"] for x in call(list_visible, m["id"], str(b["id"]))] == [pb["id"]]  # sending a task to team b
    own = call(for_user, m["id"])
    assert [x["id"] for x in own[str(a["id"])]] == [pa["id"]] and str(b["id"]) not in own
    expect_error(lambda: call(list_visible, m["id"], "nope"), 404, "NOT_FOUND")


def check_migration_adds_unassigned_to_teams_that_lack_it():
    from app.migrate import PROJECT_STATEMENTS

    t = team()
    sql("DELETE FROM statuses WHERE team_id = %s AND kind = 'unassigned'", (t["id"],))
    custom_status(t["id"], "unassigned")  # a team's own status with the clashing name

    def run():
        with tx() as conn:
            for stmt in PROJECT_STATEMENTS[-2:]:
                conn.execute(stmt)

    run()
    run()  # safe to repeat
    rows = sql("SELECT name, kind, finished FROM statuses WHERE team_id = %s ORDER BY position", (t["id"],))
    assert [(r["name"], r["kind"], r["finished"]) for r in rows] == [
        ("To do", "todo", False), ("In progress", "in_progress", False), ("Completed", "completed", True),
        ("unassigned (custom)", "custom", False), ("Unassigned", "unassigned", False)], rows


# ---- Task types -----------------------------------------------------------

def check_types_leads_and_admins_manage_members_only_read():
    from app import task_types as tt

    t = team()
    lead, m, outsider = user("lead", t["id"]), user("member", t["id"]), user()
    admin_u = make_admin()
    assert [x["name"] for x in call(tt.list_team, t["id"])] == [
        "Customer issue", "Engineering", "Payment", "Incident", "Compliance", "Task"]
    made = call(tt.create_type, lead, t["id"], tt.TypeIn(name="  Research "))
    assert made["name"] == "Research" and made["position"] == 7, made
    expect_error(lambda: call(tt.create_type, lead, t["id"], tt.TypeIn(name="research")), 409, "TYPE_EXISTS")
    expect_error(lambda: call(tt.create_type, m, t["id"], tt.TypeIn(name="Nope")), 403, "FORBIDDEN")
    expect_error(lambda: call(tt.create_type, outsider, t["id"], tt.TypeIn(name="Nope")), 404, "NOT_FOUND")
    assert call(tt.create_type, admin_u, t["id"], tt.TypeIn(name="By admin"))["name"] == "By admin"  # admins need not be members
    assert call(tt.rename_type, lead, t["id"], made["id"], tt.TypeIn(name="Discovery"))["name"] == "Discovery"
    expect_error(lambda: call(tt.rename_type, lead, t["id"], made["id"], tt.TypeIn(name="payment")), 409, "TYPE_EXISTS")
    expect_error(lambda: call(tt.rename_type, m, t["id"], made["id"], tt.TypeIn(name="X")), 403, "FORBIDDEN")
    expect_error(lambda: call(tt.rename_type, lead, t["id"], uuid.uuid4(), tt.TypeIn(name="X")), 404, "NOT_FOUND")
    expect_error(lambda: call(tt.list_team, uuid.uuid4()), 404, "NOT_FOUND")


def check_types_any_default_can_go_but_never_the_last_and_tasks_must_move():
    from app import task_types as tt

    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    incident, task = type_id(t["id"], "Incident"), type_id(t["id"], "Task")
    one, two = item(m, t["id"], type_id=incident), item(m, t["id"], type_id=incident)
    err = expect_error(lambda: call(tt.delete_type, lead, t["id"], incident, None), 409, "TYPE_IN_USE")
    assert err.details["task_count"] == 2
    expect_error(lambda: call(tt.delete_type, lead, t["id"], incident, incident), 422, "INVALID_TYPE")
    expect_error(lambda: call(tt.delete_type, m, t["id"], incident, task), 403, "FORBIDDEN")
    call(tt.delete_type, lead, t["id"], incident, task)
    for it in (one, two):
        row = sql("SELECT type_id, version FROM work_items WHERE id = %s", (it["id"],))[0]
        assert (row["type_id"], row["version"]) == (task, 2), row
        entry = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (it["id"],))[0]
        assert entry["payload"]["changes"]["type"] == {"from": "Incident", "to": "Task"}, entry
    assert sql("SELECT 1 FROM task_types WHERE id = %s", (incident,)) == []
    for name in ("Customer issue", "Engineering", "Payment", "Compliance"):  # unused defaults go freely
        call(tt.delete_type, lead, t["id"], type_id(t["id"], name), None)
    expect_error(lambda: call(tt.delete_type, lead, t["id"], task, None), 409, "LAST_TYPE")


def check_a_task_takes_only_a_type_of_its_own_team_and_types_follow_a_team_move():
    from app.items import update_item

    a, b = team(), team()
    lead = user("lead", a["id"])
    sql("UPDATE task_types SET name = 'Chore' WHERE id = %s", (type_id(b["id"], "Task"),))  # b has no "Task"
    expect_error(lambda: create(lead, a["id"], type_id=type_id(b["id"], "Payment")), 422, "INVALID_TYPE")
    expect_error(lambda: create(lead, a["id"], type_id=uuid.uuid4()), 422, "INVALID_TYPE")
    it = create(lead, a["id"], type_id=type_id(a["id"], "Payment"))
    assert it["type"] == {"id": type_id(a["id"], "Payment"), "name": "Payment"}, it["type"]
    edited = call(update_item, it["id"], lead["id"], 1, {"type_id": type_id(a["id"], "Incident")})
    assert edited["type"]["name"] == "Incident" and edited["version"] == 2
    entry = sql("SELECT payload FROM activity WHERE work_item_id = %s AND kind = 'updated'", (it["id"],))[0]
    assert entry["payload"]["changes"]["type"] == {"from": "Payment", "to": "Incident"}, entry
    expect_error(lambda: call(update_item, it["id"], lead["id"], 2, {"type_id": type_id(b["id"], "Payment")}), 422, "INVALID_TYPE")
    moved = call(update_item, it["id"], lead["id"], 2, {"team_id": b["id"]})  # b has an Incident too
    assert moved["type"]["name"] == "Incident" and moved["type"]["id"] == type_id(b["id"], "Incident")
    odd = create(lead, a["id"], type_id=type_id(a["id"], "Task"))  # b has no "Task": lands on b's first type
    got = call(update_item, odd["id"], lead["id"], 1, {"team_id": b["id"]})
    assert got["type"]["name"] == "Customer issue", got["type"]


def check_type_filter_matches_by_name_and_lists_carry_the_type():
    from app.listing import list_items

    t = team()
    lead = user("lead", t["id"])
    inc, pay = item(lead, t["id"], type_id=type_id(t["id"], "Incident")), item(lead, t["id"], type_id=type_id(t["id"], "Payment"))
    got = call(list_items, lead["id"], team_id=str(t["id"]), type=["incident"])["items"]
    assert [i["id"] for i in got] == [inc["id"]] and got[0]["type"]["name"] == "Incident", got
    both = call(list_items, lead["id"], team_id=str(t["id"]), type=["Incident", "payment"])["items"]
    assert {i["id"] for i in both} == {inc["id"], pay["id"]}
    assert call(list_items, lead["id"], team_id=str(t["id"]), type=["nonsense"])["items"] == []


def check_types_api_and_me():
    app = api_app()
    t = team()
    lead, m = user("lead", t["id"]), user("member", t["id"])
    lead_c, member_c = login_cookie(app, lead), login_cookie(app, m)
    assert call_api(app, "GET", f"/api/teams/{t['id']}/types")[0] == 401
    status, listed = call_api(app, "GET", f"/api/teams/{t['id']}/types", member_c)
    assert status == 200 and len(listed) == 6, (status, listed)
    assert call_api(app, "POST", f"/api/teams/{t['id']}/types", member_c, {"name": "X"})[0] == 403
    status, made = call_api(app, "POST", f"/api/teams/{t['id']}/types", lead_c, {"name": "Research"})
    assert status == 201 and made["name"] == "Research"
    assert call_api(app, "PATCH", f"/api/teams/{t['id']}/types/{made['id']}", lead_c, {"name": "Study"})[1]["name"] == "Study"
    assert call_api(app, "POST", f"/api/teams/{t['id']}/types", lead_c, {"name": "  "})[0] == 400
    assert call_api(app, "DELETE", f"/api/teams/{t['id']}/types/{made['id']}", lead_c)[0] == 204
    me = call_api(app, "GET", "/api/me", member_c)[1]
    assert [x["name"] for x in me["teams"][0]["types"]] == [
        "Customer issue", "Engineering", "Payment", "Incident", "Compliance", "Task"]


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
