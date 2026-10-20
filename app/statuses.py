"""Task statuses, per team.

Every team has three default statuses that can't be renamed or deleted
(To do, In progress, Completed) and any number of custom ones its leads add.
A status can be marked "finished": tasks in it stop counting as overdue, stale
or "assigned to me", and can no longer be edited, claimed or reassigned.
"""
import uuid
from typing import Annotated, Literal

from psycopg import Connection
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, StringConstraints, model_validator

from app.errors import AppError
from app.notify import publish

COLORS = ("blue", "violet", "green", "teal", "amber", "orange", "red", "pink", "slate")
# name, kind, finished, color
DEFAULTS = (
    ("To do", "todo", False, "blue"),
    ("In progress", "in_progress", False, "violet"),
    ("Completed", "completed", True, "green"),
)
StatusName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Color = Literal[COLORS]
_COLUMNS = "id, team_id, name, kind, color, finished, position"


class StatusCreate(BaseModel):
    name: StatusName
    color: Color = "slate"
    finished: bool = False


class StatusPatch(BaseModel):
    name: StatusName | None = None
    color: Color | None = None
    finished: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in ("name", "color", "finished"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class StatusOrder(BaseModel):
    ids: list[uuid.UUID]


def create_defaults(conn: Connection, team_id) -> None:
    for position, (name, kind, finished, color) in enumerate(DEFAULTS, start=1):
        conn.execute(
            "INSERT INTO statuses (team_id, name, kind, color, finished, position) VALUES (%s, %s, %s, %s, %s, %s)",
            (team_id, name, kind, color, finished, position),
        )


def dto(row: dict) -> dict:
    return {k: row[k] for k in ("id", "name", "kind", "color", "finished", "position")}


def _uuid_or_404(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise AppError(404, "NOT_FOUND", f"{what} not found.")


def _role(conn: Connection, user_id, team_id) -> str | None:
    row = conn.execute("SELECT role FROM memberships WHERE user_id = %s AND team_id = %s",
                       (user_id, team_id)).fetchone()
    return row["role"] if row else None


def _require_member(conn: Connection, user_id, team_id) -> uuid.UUID:
    team_id = _uuid_or_404(team_id, "Team")
    if _role(conn, user_id, team_id) is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    return team_id


def _require_lead(conn: Connection, user_id, team_id) -> uuid.UUID:
    team_id = _require_member(conn, user_id, team_id)
    if _role(conn, user_id, team_id) != "lead":
        raise AppError(403, "FORBIDDEN", "Only team leads can manage statuses.", {"permission": "manage_statuses"})
    return team_id


def list_team(conn: Connection, user_id, team_id) -> list[dict]:
    team_id = _require_member(conn, user_id, team_id)
    rows = conn.execute(f"SELECT {_COLUMNS} FROM statuses WHERE team_id = %s ORDER BY position", (team_id,))
    return [dto(r) for r in rows]


def for_user(conn: Connection, user_id) -> dict[str, list[dict]]:
    """Statuses of every team the user is in, keyed by team id (for /api/me)."""
    rows = conn.execute(
        """SELECT s.id, s.team_id, s.name, s.kind, s.color, s.finished, s.position
           FROM statuses s JOIN memberships m ON m.team_id = s.team_id AND m.user_id = %s
           ORDER BY s.team_id, s.position""", (user_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r["team_id"]), []).append(dto(r))
    return out


def default_status_id(conn: Connection, team_id, kind: str = "todo"):
    return conn.execute("SELECT id FROM statuses WHERE team_id = %s AND kind = %s", (team_id, kind)).fetchone()["id"]


def equivalent_in(conn: Connection, status_id, dest_team_id):
    """Where a task's status lands when the task moves to another team: the same
    default status, or a custom one with the same name, otherwise To do."""
    s = conn.execute("SELECT name, kind FROM statuses WHERE id = %s", (status_id,)).fetchone()
    if s["kind"] != "custom":
        return default_status_id(conn, dest_team_id, s["kind"])
    same_name = conn.execute("SELECT id FROM statuses WHERE team_id = %s AND lower(name) = lower(%s)",
                             (dest_team_id, s["name"])).fetchone()
    return same_name["id"] if same_name else default_status_id(conn, dest_team_id, "todo")


def _changed(conn: Connection, team_id, actor_id) -> None:
    publish(conn, {"kind": "statuses", "team_ids": [str(team_id)], "actor_id": str(actor_id)})


def create_status(conn: Connection, user_id, team_id, data: StatusCreate) -> dict:
    team_id = _require_lead(conn, user_id, team_id)
    position = conn.execute("SELECT coalesce(max(position), 0) + 1 AS p FROM statuses WHERE team_id = %s",
                            (team_id,)).fetchone()["p"]
    try:
        row = conn.execute(
            f"""INSERT INTO statuses (team_id, name, kind, color, finished, position)
                VALUES (%s, %s, 'custom', %s, %s, %s) RETURNING {_COLUMNS}""",
            (team_id, data.name, data.color, data.finished, position)).fetchone()
    except UniqueViolation:
        raise AppError(409, "STATUS_EXISTS", f"This team already has a status called \"{data.name}\".")
    _changed(conn, team_id, user_id)
    return dto(row)


def _load(conn: Connection, team_id, status_id) -> dict:
    status_id = _uuid_or_404(status_id, "Status")
    row = conn.execute(f"SELECT {_COLUMNS} FROM statuses WHERE id = %s AND team_id = %s",
                       (status_id, team_id)).fetchone()
    if row is None:
        raise AppError(404, "NOT_FOUND", "Status not found.")
    return row


def update_status(conn: Connection, user_id, team_id, status_id, data: StatusPatch) -> dict:
    team_id = _require_lead(conn, user_id, team_id)
    row = _load(conn, team_id, status_id)
    changes = {f: getattr(data, f) for f in ("name", "color", "finished") if f in data.model_fields_set}
    if row["kind"] != "custom" and (changes.get("name", row["name"]) != row["name"]
                                    or changes.get("finished", row["finished"]) != row["finished"]):
        raise AppError(403, "DEFAULT_STATUS", f"\"{row['name']}\" is a default status: its name and whether it "
                       "counts as finished can't be changed.")
    changes = {f: v for f, v in changes.items() if row[f] != v}
    if not changes:
        return dto(row)
    sets = ", ".join(f"{f} = %s" for f in changes)  # names come from the fixed tuple above
    try:
        row = conn.execute(f"UPDATE statuses SET {sets} WHERE id = %s RETURNING {_COLUMNS}",
                           (*changes.values(), row["id"])).fetchone()
    except UniqueViolation:
        raise AppError(409, "STATUS_EXISTS", "This team already has a status with that name.")
    _changed(conn, team_id, user_id)
    return dto(row)


def reorder(conn: Connection, user_id, team_id, ids: list[uuid.UUID]) -> list[dict]:
    team_id = _require_lead(conn, user_id, team_id)
    current = {r["id"] for r in conn.execute("SELECT id FROM statuses WHERE team_id = %s", (team_id,))}
    if len(ids) != len(set(ids)) or set(ids) != current:
        raise AppError(422, "INVALID_ORDER", "List every status of the team exactly once.")
    for position, status_id in enumerate(ids, start=1):
        conn.execute("UPDATE statuses SET position = %s WHERE id = %s", (position, status_id))
    _changed(conn, team_id, user_id)
    return list_team(conn, user_id, team_id)


def delete_status(conn: Connection, user_id, team_id, status_id, move_to) -> None:
    """Delete a custom status. Its tasks must go somewhere: `move_to` is required
    when there are any, and each moved task gets a history entry."""
    team_id = _require_lead(conn, user_id, team_id)
    row = _load(conn, team_id, status_id)
    if row["kind"] != "custom":
        raise AppError(403, "DEFAULT_STATUS", f"\"{row['name']}\" is a default status and can't be deleted.")
    count = conn.execute("SELECT count(*) AS n FROM work_items WHERE status_id = %s", (row["id"],)).fetchone()["n"]
    if count:
        if move_to is None:
            raise AppError(409, "STATUS_IN_USE", f"{count} task(s) are in \"{row['name']}\". Choose where they should go.",
                           {"task_count": count})
        target = _load(conn, team_id, move_to)
        if target["id"] == row["id"]:
            raise AppError(422, "INVALID_STATUS", "Choose a different status for these tasks.")
        conn.execute(
            """INSERT INTO activity (work_item_id, actor_id, kind, payload)
               SELECT id, %s, 'status_changed', jsonb_build_object('from', %s::text, 'to', %s::text,
                      'to_color', %s::text, 'reason', 'Status deleted')
               FROM work_items WHERE status_id = %s""",
            (user_id, row["name"], target["name"], target["color"], row["id"]))
        conn.execute(
            """UPDATE work_items SET status_id = %s, version = version + 1, updated_at = now(),
                      last_activity_at = now() WHERE status_id = %s""", (target["id"], row["id"]))
    conn.execute("DELETE FROM statuses WHERE id = %s", (row["id"],))
    _changed(conn, team_id, user_id)
