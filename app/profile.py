"""The signed-in user's profile: account details, teams and a work summary."""
from typing import Annotated

from fastapi import APIRouter
from psycopg import Connection
from pydantic import BaseModel, StringConstraints

from app.auth import Name, User, hash_password, verify_password
from app.db import Conn
from app.errors import AppError

router = APIRouter(prefix="/api/profile")


class ProfilePatch(BaseModel):
    name: Name


class PasswordChange(BaseModel):
    current_password: str
    new_password: Annotated[str, StringConstraints(min_length=8, max_length=200)]


def get_profile(conn: Connection, user_id) -> dict:
    user = conn.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,)).fetchone()
    teams = conn.execute(
        """SELECT t.id, t.name, m.role,
                  (SELECT count(*) FROM memberships x WHERE x.team_id = t.id) AS member_count,
                  (SELECT count(*) FROM work_items w WHERE w.team_id = t.id) AS total_tasks,
                  (SELECT count(*) FROM work_items w JOIN statuses s ON s.id = w.status_id
                    WHERE w.team_id = t.id AND NOT s.finished AND w.approval IS DISTINCT FROM 'rejected') AS open_tasks
           FROM memberships m JOIN teams t ON t.id = m.team_id
           WHERE m.user_id = %s ORDER BY t.name""", (user_id,)).fetchall()
    members: dict = {}
    if teams:
        rows = conn.execute(
            """SELECT m.team_id, u.id, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id
               WHERE m.team_id = ANY(%s) ORDER BY (m.role = 'lead') DESC, u.name""",
            ([t["id"] for t in teams],)).fetchall()
        for r in rows:
            members.setdefault(r["team_id"], []).append({"id": r["id"], "name": r["name"], "role": r["role"]})
    stats = conn.execute(
        """SELECT count(*) FILTER (WHERE w.assignee_id = %(u)s AND NOT closed) AS assigned,
                  count(*) FILTER (WHERE w.assignee_id = %(u)s AND NOT closed AND w.due_at < now()) AS overdue,
                  count(*) FILTER (WHERE w.assignee_id = %(u)s AND finished) AS finished,
                  count(*) FILTER (WHERE w.created_by = %(u)s) AS created
           FROM (SELECT w.*, s.finished,
                        (s.finished OR COALESCE(w.approval = 'rejected', false)) AS closed
                 FROM work_items w JOIN statuses s ON s.id = w.status_id
                 WHERE w.assignee_id = %(u)s OR w.created_by = %(u)s) w""", {"u": user_id}).fetchone()
    return {
        "user": user,
        "teams": [{**t, "members": members.get(t["id"], [])} for t in teams],
        "stats": stats,
    }


def update_profile(conn: Connection, user_id, data: ProfilePatch) -> dict:
    conn.execute("UPDATE users SET name = %s WHERE id = %s", (data.name, user_id))
    return get_profile(conn, user_id)


def change_password(conn: Connection, user_id, data: PasswordChange) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,)).fetchone()
    if not verify_password(data.current_password, row["password_hash"]):
        raise AppError(422, "WRONG_PASSWORD", "Your current password is incorrect.")
    if data.new_password == data.current_password:
        raise AppError(422, "SAME_PASSWORD", "Choose a password different from the current one.")
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(data.new_password), user_id))


@router.get("")
def read(user: User, conn: Conn):
    return get_profile(conn, user["id"])


@router.patch("")
def update(body: ProfilePatch, user: User, conn: Conn):
    return update_profile(conn, user["id"], body)


@router.post("/password", status_code=204)
def password(body: PasswordChange, user: User, conn: Conn):
    change_password(conn, user["id"], body)
