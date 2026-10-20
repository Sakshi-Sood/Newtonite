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


def user(role: str | None = None, team_id=None, name: str | None = None) -> dict:
    from app.auth import hash_password

    n = _next()
    row = sql(
        "INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s) RETURNING *",
        (f"{RUN}-{n}@check.test", name or f"Check User {n}", hash_password("password")),
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


def check_outsider_create_and_get_404():
    from app.items import get_item

    t = team()
    m = user("member", t["id"])
    outsider = user()
    expect_error(lambda: create(outsider, t["id"]), 404, "NOT_FOUND")
    it = create(m, t["id"])
    expect_error(lambda: call(get_item, it["id"], outsider["id"]), 404, "NOT_FOUND")


def check_membership_removed_404():
    from app.items import get_item

    t = team()
    m = user("member", t["id"])
    it = create(m, t["id"])
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
    assert err.details["changed_by"] == "First Editor"
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
