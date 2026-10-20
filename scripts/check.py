"""Behaviour checks against the real database: python scripts/check.py

Each run creates its own uniquely named team(s) and users, runs every
`check_*` function in this file, prints PASS/FAIL per check, and deletes
everything it created (even when a check fails). Exit code 1 if any failed.
"""
import sys
import traceback
import uuid
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
