"""Finding work: filtered/sorted/searched lists, attention views, overview and
board counts.

Everything happens in SQL and is limited to what the caller may see: items in
their teams plus items they requested from other teams. Nothing loads the
whole table into memory.
"""
import uuid

from psycopg import Connection

from app.errors import AppError
from app.rules import PRIORITIES, allowed_actions

# Every query below joins statuses as `s`. A task is "open" while its status isn't
# finished and a lead hasn't rejected it.
NOT_CLOSED = "(NOT s.finished AND w.approval IS DISTINCT FROM 'rejected')"
VIEWS = ("mine", "unassigned_urgent", "awaiting_my_approval", "overdue", "stale", "requested_by_me")
SORTS = {
    "priority": "w.priority, w.last_activity_at DESC, w.id",
    "updated": "w.last_activity_at DESC, w.id",
    "created": "w.created_at DESC, w.id",
}

_LIST_COLUMNS = """
    w.id, w.team_id, t.name AS team_name, w.title, w.type_id, ty.name AS type_name, w.priority, w.project_id, pr.name AS project_name,
    s.id AS status_id, s.name AS status_name, s.kind AS status_kind, s.color AS status_color,
    s.finished AS status_finished, (s.finished OR COALESCE(w.approval = 'rejected', false)) AS closed,
    w.requires_approval, w.approval, w.restricted, w.due_at, w.assignee_id, a.name AS assignee_name,
    w.assigned_by, w.created_by, c.name AS creator_name, w.created_at, w.updated_at,
    w.last_activity_at, w.version, mm.role AS team_role
"""
_LIST_FROM = """
    FROM work_items w
    JOIN statuses s ON s.id = w.status_id
    JOIN teams t ON t.id = w.team_id
    LEFT JOIN projects pr ON pr.id = w.project_id
    JOIN task_types ty ON ty.id = w.type_id
    JOIN users c ON c.id = w.created_by
    LEFT JOIN users a ON a.id = w.assignee_id
    LEFT JOIN memberships mm ON mm.team_id = w.team_id AND mm.user_id = %s
"""
# What the caller may see: their teams' items, plus items they requested elsewhere.
_SCOPE = "(w.team_id IN (SELECT team_id FROM memberships WHERE user_id = %s) OR w.created_by = %s)"


def view_condition(view: str, user_id) -> tuple[str, list]:
    """SQL condition (on aliases w and s) for one attention view. Shared by lists and counts."""
    if view == "mine":
        return f"w.assignee_id = %s AND {NOT_CLOSED}", [user_id]
    if view == "unassigned_urgent":
        return f"w.assignee_id IS NULL AND w.priority IN ('urgent', 'high') AND {NOT_CLOSED}", []
    if view == "awaiting_my_approval":
        return ("""w.approval = 'pending' AND w.created_by <> %s AND EXISTS (
                     SELECT 1 FROM memberships lm
                     WHERE lm.user_id = %s AND lm.team_id = w.team_id AND lm.role = 'lead')""",
                [user_id, user_id])
    if view == "overdue":
        return f"w.due_at < now() AND {NOT_CLOSED}", []
    if view == "stale":
        return f"w.last_activity_at < now() - interval '72 hours' AND {NOT_CLOSED}", []
    if view == "requested_by_me":
        return ("""w.created_by = %s AND NOT EXISTS (
                     SELECT 1 FROM memberships rm WHERE rm.user_id = %s AND rm.team_id = w.team_id)""",
                [user_id, user_id])
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


def _filters(user_id, *, view=None, team_id=None, project=None, status=(), status_id=None, priority=(),
             type=(), assignee=None, q=None) -> tuple[list[str], list]:
    """WHERE conditions and their parameters, shared by lists and board counts.

    `project` is a project id, or "none" for tasks without one.
    `status` filters by status *name* (so "Blocked" matches that status in every
    team that has one); `status_id` picks one exact status of one team."""
    where, params = [_SCOPE], [user_id, user_id]
    if view:
        cond, p = view_condition(view, user_id)
        where.append(cond)
        params += p
    if team_id:
        where.append("w.team_id = %s")
        params.append(_as_uuid(team_id, "team_id"))
    if project == "none":
        where.append("w.project_id IS NULL")
    elif project:
        where.append("w.project_id = %s")
        params.append(_as_uuid(project, "project"))
    if status:
        names = [n.strip().lower() for n in status if n.strip()]
        if any(len(n) > 40 for n in names):
            raise AppError(400, "INVALID_FILTER", "Status names are at most 40 characters.")
        where.append("lower(s.name) = ANY(%s)")
        params.append(names)
    if status_id:
        where.append("w.status_id = %s")
        params.append(_as_uuid(status_id, "status_id"))
    if priority:
        _check_values("priority", list(priority), PRIORITIES)
        where.append("w.priority::text = ANY(%s)")
        params.append(list(priority))
    if type:
        # By name, like statuses: "Incident" matches that type in every team that has one.
        where.append("w.type_id IN (SELECT id FROM task_types WHERE lower(name) = ANY(%s))")
        params.append([n.strip().lower() for n in type if n.strip()])
    if assignee == "me":
        where.append("w.assignee_id = %s")
        params.append(user_id)
    elif assignee == "unassigned":
        where.append("w.assignee_id IS NULL")
    elif assignee:
        where.append("w.assignee_id = %s")
        params.append(_as_uuid(assignee, "assignee"))
    q = (q or "").strip()
    if q:
        # websearch_to_tsquery accepts any user text without syntax errors.
        where.append("w.search @@ websearch_to_tsquery('english', %s)")
        params.append(q)
    return where, params


def build_list_query(user_id, *, sort=None, page=1, page_size=25, **filters) -> tuple[str, list]:
    where, params = _filters(user_id, **filters)
    q = (filters.get("q") or "").strip()
    order_params: list = []
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
    return query, [user_id] + params + order_params + [page_size + 1, (page - 1) * page_size]


def _list_dto(r: dict, user_id) -> dict:
    role = r["team_role"] or ("requester" if str(r["created_by"]) == str(user_id) else None)
    return {
        "id": r["id"],
        "team": {"id": r["team_id"], "name": r["team_name"]},
        "title": r["title"], "type": {"id": r["type_id"], "name": r["type_name"]}, "priority": r["priority"],
        "project": {"id": r["project_id"], "name": r["project_name"]} if r["project_id"] else None,
        "status": {"id": r["status_id"], "name": r["status_name"], "kind": r["status_kind"],
                   "color": r["status_color"], "finished": r["status_finished"]},
        "requires_approval": r["requires_approval"], "approval": r["approval"], "closed": r["closed"],
        "restricted": r["restricted"], "due_at": r["due_at"],
        "assignee": {"id": r["assignee_id"], "name": r["assignee_name"]} if r["assignee_id"] else None,
        "created_by": {"id": r["created_by"], "name": r["creator_name"]},
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "last_activity_at": r["last_activity_at"], "version": r["version"],
        "my_role": role,
        # Lets the status dropdown and the board offer only moves that will work.
        "allowed_actions": allowed_actions(r, user_id, role),
    }


def list_items(conn: Connection, user_id, *, page: int = 1, page_size: int = 25, **filters) -> dict:
    query, params = build_list_query(user_id, page=page, page_size=page_size, **filters)
    rows = conn.execute(query, params).fetchall()
    return {
        "items": [_list_dto(r, user_id) for r in rows[:page_size]],
        "page": page,
        "page_size": page_size,
        "has_more": len(rows) > page_size,
    }


def status_counts(conn: Connection, user_id, *, team_id, **filters) -> dict[str, int]:
    """How many tasks of one team match the filters in each of its statuses
    (board column headers), keyed by status id. Statuses with none are omitted."""
    if not team_id:
        raise AppError(400, "TEAM_REQUIRED", "Pick a team: every team has its own statuses.")
    filters.pop("status", None)
    filters.pop("status_id", None)
    where, params = _filters(user_id, team_id=team_id, **filters)
    rows = conn.execute(
        f"""SELECT w.status_id::text AS id, count(*) AS n
            FROM work_items w JOIN statuses s ON s.id = w.status_id
            WHERE {' AND '.join(where)} GROUP BY w.status_id""", params,
    ).fetchall()
    return {r["id"]: r["n"] for r in rows}


def overview_counts(conn: Connection, user_id) -> dict:
    """All attention counts in one pass over the caller's items."""
    parts, params = [], []
    for view in VIEWS:
        cond, p = view_condition(view, user_id)
        parts.append(f"count(*) FILTER (WHERE {cond}) AS {view}")
        params += p
    return conn.execute(
        f"""SELECT {', '.join(parts)} FROM work_items w JOIN statuses s ON s.id = w.status_id
            WHERE {_SCOPE}""", params + [user_id, user_id]
    ).fetchone()
