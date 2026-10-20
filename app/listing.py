"""Finding work: filtered/sorted/searched lists, attention views, overview counts.

Everything happens in SQL and is limited to the caller's teams; nothing loads
the whole table into memory.
"""
import uuid

from psycopg import Connection

from app.errors import AppError
from app.rules import ITEM_TYPES, PRIORITIES, STATUSES

NOT_TERMINAL = "w.status NOT IN ('done', 'cancelled')"
VIEWS = ("mine", "unassigned_urgent", "awaiting_my_approval", "overdue", "stale")
SORTS = {
    "priority": "w.priority, w.last_activity_at DESC, w.id",
    "updated": "w.last_activity_at DESC, w.id",
    "created": "w.created_at DESC, w.id",
}

_LIST_COLUMNS = """
    w.id, w.team_id, t.name AS team_name, w.title, w.type, w.priority, w.status,
    w.requires_approval, w.due_at, w.assignee_id, a.name AS assignee_name,
    w.created_by, c.name AS creator_name, w.created_at, w.updated_at,
    w.last_activity_at, w.version
"""
_LIST_FROM = """
    FROM work_items w
    JOIN teams t ON t.id = w.team_id
    JOIN users c ON c.id = w.created_by
    LEFT JOIN users a ON a.id = w.assignee_id
"""
_SCOPE = "w.team_id IN (SELECT team_id FROM memberships WHERE user_id = %s)"


def view_condition(view: str, user_id) -> tuple[str, list]:
    """SQL condition (on alias w) for one attention view. Shared by lists and counts."""
    if view == "mine":
        return f"w.assignee_id = %s AND {NOT_TERMINAL}", [user_id]
    if view == "unassigned_urgent":
        return f"w.assignee_id IS NULL AND w.priority IN ('urgent', 'high') AND {NOT_TERMINAL}", []
    if view == "awaiting_my_approval":
        return ("""w.status = 'awaiting_approval' AND w.created_by <> %s AND EXISTS (
                     SELECT 1 FROM memberships lm
                     WHERE lm.user_id = %s AND lm.team_id = w.team_id AND lm.role = 'lead')""",
                [user_id, user_id])
    if view == "overdue":
        return f"w.due_at < now() AND {NOT_TERMINAL}", []
    if view == "stale":
        return f"w.last_activity_at < now() - interval '72 hours' AND {NOT_TERMINAL}", []
    raise AppError(400, "INVALID_VIEW", f"Unknown view '{view}'.", {"allowed": list(VIEWS)})


def _check_values(name: str, values: list[str], allowed: tuple) -> None:
    bad = [v for v in values if v not in allowed]
    if bad:
        raise AppError(400, "INVALID_FILTER", f"Unknown {name}: {', '.join(bad)}.", {"allowed": list(allowed)})


def _as_uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AppError(400, "INVALID_FILTER", f"{name} must be an id.")


def build_list_query(user_id, *, view=None, team_id=None, status=(), priority=(), type=(),
                     assignee=None, q=None, sort=None, page=1, page_size=25) -> tuple[str, list]:
    where, params = [_SCOPE], [user_id]
    if view:
        cond, p = view_condition(view, user_id)
        where.append(cond)
        params += p
    if team_id:
        where.append("w.team_id = %s")
        params.append(_as_uuid(team_id, "team_id"))
    for column, values, allowed in (("status", status, STATUSES), ("priority", priority, PRIORITIES),
                                    ("type", type, ITEM_TYPES)):
        if values:
            _check_values(column, list(values), allowed)
            where.append(f"w.{column}::text = ANY(%s)")
            params.append(list(values))
    if assignee == "me":
        where.append("w.assignee_id = %s")
        params.append(user_id)
    elif assignee == "unassigned":
        where.append("w.assignee_id IS NULL")
    elif assignee:
        where.append("w.assignee_id = %s")
        params.append(_as_uuid(assignee, "assignee"))

    q = (q or "").strip()
    order_params: list = []
    if q:
        # websearch_to_tsquery accepts any user text without syntax errors.
        where.append("w.search @@ websearch_to_tsquery('english', %s)")
        params.append(q)
    if sort is None and q:
        order = "ts_rank(w.search, websearch_to_tsquery('english', %s)) DESC, w.id"
        order_params = [q]
    else:
        sort = sort or "priority"
        if sort not in SORTS:
            raise AppError(400, "INVALID_SORT", f"Unknown sort '{sort}'.", {"allowed": list(SORTS)})
        order = SORTS[sort]

    query = (f"SELECT {_LIST_COLUMNS} {_LIST_FROM} WHERE {' AND '.join(where)} "
             f"ORDER BY {order} LIMIT %s OFFSET %s")
    # One extra row tells us whether there is a next page, without a COUNT(*).
    return query, params + order_params + [page_size + 1, (page - 1) * page_size]


def _list_dto(r: dict) -> dict:
    return {
        "id": r["id"],
        "team": {"id": r["team_id"], "name": r["team_name"]},
        "title": r["title"], "type": r["type"], "priority": r["priority"], "status": r["status"],
        "requires_approval": r["requires_approval"], "due_at": r["due_at"],
        "assignee": {"id": r["assignee_id"], "name": r["assignee_name"]} if r["assignee_id"] else None,
        "created_by": {"id": r["created_by"], "name": r["creator_name"]},
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "last_activity_at": r["last_activity_at"], "version": r["version"],
    }


def list_items(conn: Connection, user_id, *, page: int = 1, page_size: int = 25, **filters) -> dict:
    query, params = build_list_query(user_id, page=page, page_size=page_size, **filters)
    rows = conn.execute(query, params).fetchall()
    return {
        "items": [_list_dto(r) for r in rows[:page_size]],
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


def overview_counts(conn: Connection, user_id) -> dict:
    """All five attention counts in one pass over the caller's items."""
    parts, params = [], []
    for view in VIEWS:
        cond, p = view_condition(view, user_id)
        parts.append(f"count(*) FILTER (WHERE {cond}) AS {view}")
        params += p
    return conn.execute(
        f"SELECT {', '.join(parts)} FROM work_items w WHERE {_SCOPE}", params + [user_id]
    ).fetchone()
