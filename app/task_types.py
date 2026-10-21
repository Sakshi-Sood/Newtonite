"""Task types, per team.

Every team starts with the same six types (Customer issue, Engineering, Payment,
Incident, Compliance, Task). None is special: the team's leads (and admins) can
rename, add and remove any of them. A task always has a type, so the last one
can't be removed, and removing a type that tasks use means choosing another type
for those tasks (each gets a history entry).
"""
import uuid
from typing import Annotated

from psycopg import Connection
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, StringConstraints

from app.errors import AppError
from app.notify import publish

DEFAULTS = ("Customer issue", "Engineering", "Payment", "Incident", "Compliance", "Task")
TypeName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class TypeIn(BaseModel):
    name: TypeName


def create_defaults(conn: Connection, team_id) -> None:
    for position, name in enumerate(DEFAULTS, start=1):
        conn.execute("INSERT INTO task_types (team_id, name, position) VALUES (%s, %s, %s)", (team_id, name, position))


def dto(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "position": row["position"]}


def _uuid_or_404(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise AppError(404, "NOT_FOUND", f"{what} not found.")


def _team_or_404(conn: Connection, team_id) -> dict:
    row = conn.execute("SELECT id, name FROM teams WHERE id = %s", (_uuid_or_404(team_id, "Team"),)).fetchone()
    if row is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    return row


def _require_manager(conn: Connection, user: dict, team_id) -> uuid.UUID:
    """Leads of the team and admins may change its types; other members may only read them."""
    team = _team_or_404(conn, team_id)
    if user.get("is_admin"):
        return team["id"]
    role = conn.execute("SELECT role FROM memberships WHERE user_id = %s AND team_id = %s",
                        (user["id"], team["id"])).fetchone()
    if role is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    if role["role"] != "lead":
        raise AppError(403, "FORBIDDEN", "Only team leads can manage task types.", {"permission": "manage_types"})
    return team["id"]


def list_team(conn: Connection, team_id) -> list[dict]:
    """A team's types. Any signed-in user may read them: sending a task to another team needs its list."""
    team = _team_or_404(conn, team_id)
    rows = conn.execute("SELECT id, name, position FROM task_types WHERE team_id = %s ORDER BY position, id",
                        (team["id"],)).fetchall()
    return [dto(r) for r in rows]


def for_user(conn: Connection, user_id) -> dict[str, list[dict]]:
    """Types of every team the user is in, keyed by team id (for /api/me)."""
    rows = conn.execute(
        """SELECT ty.id, ty.team_id, ty.name, ty.position FROM task_types ty
           JOIN memberships m ON m.team_id = ty.team_id AND m.user_id = %s
           ORDER BY ty.team_id, ty.position, ty.id""", (user_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r["team_id"]), []).append(dto(r))
    return out


def require_in_team(conn: Connection, team_id, type_id) -> None:
    ok = conn.execute("SELECT 1 FROM task_types WHERE id = %s AND team_id = %s", (type_id, team_id)).fetchone()
    if ok is None:
        raise AppError(422, "INVALID_TYPE", "That type doesn't belong to the task's team.")


def equivalent_in(conn: Connection, type_id, dest_team_id):
    """Where a task's type lands when the task moves to another team: the type with the same
    name there, otherwise that team's first type."""
    same = conn.execute(
        """SELECT d.id FROM task_types s JOIN task_types d ON d.team_id = %s AND lower(d.name) = lower(s.name)
           WHERE s.id = %s""", (dest_team_id, type_id)).fetchone()
    if same:
        return same["id"]
    return conn.execute("SELECT id FROM task_types WHERE team_id = %s ORDER BY position, id LIMIT 1",
                        (dest_team_id,)).fetchone()["id"]


def name_of(conn: Connection, type_id) -> str:
    return conn.execute("SELECT name FROM task_types WHERE id = %s", (type_id,)).fetchone()["name"]


def _changed(conn: Connection, team_id, actor_id) -> None:
    publish(conn, {"kind": "statuses", "team_ids": [str(team_id)], "actor_id": str(actor_id)})


def create_type(conn: Connection, user: dict, team_id, data: TypeIn) -> dict:
    team_id = _require_manager(conn, user, team_id)
    position = conn.execute("SELECT coalesce(max(position), 0) + 1 AS p FROM task_types WHERE team_id = %s",
                            (team_id,)).fetchone()["p"]
    try:
        row = conn.execute("INSERT INTO task_types (team_id, name, position) VALUES (%s, %s, %s) "
                           "RETURNING id, name, position", (team_id, data.name, position)).fetchone()
    except UniqueViolation:
        raise AppError(409, "TYPE_EXISTS", f"This team already has a type called \"{data.name}\".")
    _changed(conn, team_id, user["id"])
    return dto(row)


def _load(conn: Connection, team_id, type_id) -> dict:
    row = conn.execute("SELECT id, name, position FROM task_types WHERE id = %s AND team_id = %s",
                       (_uuid_or_404(type_id, "Type"), team_id)).fetchone()
    if row is None:
        raise AppError(404, "NOT_FOUND", "Type not found.")
    return row


def rename_type(conn: Connection, user: dict, team_id, type_id, data: TypeIn) -> dict:
    team_id = _require_manager(conn, user, team_id)
    row = _load(conn, team_id, type_id)
    if row["name"] == data.name:
        return dto(row)
    try:
        row = conn.execute("UPDATE task_types SET name = %s WHERE id = %s RETURNING id, name, position",
                           (data.name, row["id"])).fetchone()
    except UniqueViolation:
        raise AppError(409, "TYPE_EXISTS", f"This team already has a type called \"{data.name}\".")
    _changed(conn, team_id, user["id"])
    return dto(row)


def delete_type(conn: Connection, user: dict, team_id, type_id, move_to) -> None:
    """Remove a type. Tasks of that type must go to another type of the team (`move_to`)."""
    team_id = _require_manager(conn, user, team_id)
    row = _load(conn, team_id, type_id)
    if conn.execute("SELECT count(*) AS n FROM task_types WHERE team_id = %s", (team_id,)).fetchone()["n"] <= 1:
        raise AppError(409, "LAST_TYPE", "A team needs at least one task type, so the last one can't be removed.")
    count = conn.execute("SELECT count(*) AS n FROM work_items WHERE type_id = %s", (row["id"],)).fetchone()["n"]
    if count:
        if move_to is None:
            raise AppError(409, "TYPE_IN_USE", f"{count} task(s) are of type \"{row['name']}\". Choose a type for them.",
                           {"task_count": count})
        target = _load(conn, team_id, move_to)
        if target["id"] == row["id"]:
            raise AppError(422, "INVALID_TYPE", "Choose a different type for these tasks.")
        conn.execute(
            """INSERT INTO activity (work_item_id, actor_id, kind, payload)
               SELECT id, %s, 'updated', jsonb_build_object('changes', jsonb_build_object('type',
                      jsonb_build_object('from', %s::text, 'to', %s::text)))
               FROM work_items WHERE type_id = %s""", (user["id"], row["name"], target["name"], row["id"]))
        conn.execute(
            """UPDATE work_items SET type_id = %s, version = version + 1, updated_at = now(),
                      last_activity_at = now() WHERE type_id = %s""", (target["id"], row["id"]))
    conn.execute("DELETE FROM task_types WHERE id = %s", (row["id"],))
    _changed(conn, team_id, user["id"])
