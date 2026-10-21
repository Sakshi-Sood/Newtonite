"""Admin operations: teams, users and membership.

Only admins reach these (see admin_routes). Rules kept here:
- a team that has members always has at least one lead;
- the last active admin can't be demoted or deactivated, and nobody can deactivate themselves;
- a new user, or a user whose password an admin reset, holds a temporary password
  and must choose their own at the next login;
- removing someone from a team unassigns their tasks there (each gets a history entry).
"""
import uuid
from typing import Literal

from psycopg import Connection
from pydantic import BaseModel, model_validator

from app import statuses, task_types
from app.auth import Email, Name, Password, hash_password
from app.errors import AppError

Role = Literal["member", "lead"]
_USER_COLUMNS = "id, name, email, is_admin, active, must_change_password"


class TeamIn(BaseModel):
    name: Name


class TeamDelete(BaseModel):
    confirm_name: str


class MembershipIn(BaseModel):
    team_id: uuid.UUID
    role: Role = "member"


class RoleIn(BaseModel):
    role: Role


class UserCreate(BaseModel):
    name: Name
    email: Email
    temporary_password: Password
    is_admin: bool = False
    memberships: list[MembershipIn] = []


class UserPatch(BaseModel):
    name: Name | None = None
    email: Email | None = None
    is_admin: bool | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class PasswordReset(BaseModel):
    temporary_password: Password


def _uuid_or_404(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise AppError(404, "NOT_FOUND", f"{what} not found.")


def _find(conn: Connection, table: str, value, what: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (_uuid_or_404(value, what),)).fetchone()  # table is fixed by callers
    if row is None:
        raise AppError(404, "NOT_FOUND", f"{what} not found.")
    return row


# ------------------------------------------------------------------ overview

def summary(conn: Connection) -> dict:
    return conn.execute(
        """SELECT (SELECT count(*) FROM users) AS users,
                  (SELECT count(*) FROM users WHERE active) AS active_users,
                  (SELECT count(*) FROM users WHERE is_admin AND active) AS admins,
                  (SELECT count(*) FROM teams) AS teams""").fetchone()


# --------------------------------------------------------------------- teams

def team_options(conn: Connection) -> list[dict]:
    return conn.execute("SELECT id, name FROM teams ORDER BY lower(name), id").fetchall()


def list_teams(conn: Connection, q: str | None, page: int, page_size: int) -> dict:
    q = (q or "").strip().lower()
    rows = conn.execute(
        """SELECT t.id, t.name,
                  (SELECT count(*) FROM memberships m WHERE m.team_id = t.id) AS member_count,
                  (SELECT count(*) FROM memberships m WHERE m.team_id = t.id AND m.role = 'lead') AS lead_count,
                  (SELECT coalesce(string_agg(u.name, ', ' ORDER BY u.name), '') FROM memberships m
                     JOIN users u ON u.id = m.user_id WHERE m.team_id = t.id AND m.role = 'lead') AS lead_names,
                  (SELECT count(*) FROM work_items w WHERE w.team_id = t.id) AS total_tasks,
                  (SELECT count(*) FROM work_items w JOIN statuses s ON s.id = w.status_id
                    WHERE w.team_id = t.id AND NOT s.finished AND w.approval IS DISTINCT FROM 'rejected') AS open_tasks
           FROM teams t
           WHERE %s = '' OR position(%s in lower(t.name)) > 0
           ORDER BY lower(t.name), t.id LIMIT %s OFFSET %s""",
        (q, q, page_size + 1, (page - 1) * page_size)).fetchall()
    return {"items": rows[:page_size], "page": page, "has_more": len(rows) > page_size}


def create_team(conn: Connection, data: TeamIn) -> dict:
    row = conn.execute("INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id, name",
                       (data.name,)).fetchone()
    if row is None:
        raise AppError(409, "TEAM_NAME_TAKEN", f"A team called \"{data.name}\" already exists.")
    statuses.create_defaults(conn, row["id"])
    task_types.create_defaults(conn, row["id"])
    return {**row, "member_count": 0, "lead_count": 0, "lead_names": "", "total_tasks": 0, "open_tasks": 0}


def rename_team(conn: Connection, team_id, data: TeamIn) -> dict:
    team = _find(conn, "teams", team_id, "Team")
    if team["name"] == data.name:
        return {"id": team["id"], "name": team["name"]}
    row = conn.execute("UPDATE teams SET name = %s WHERE id = %s AND NOT EXISTS "
                       "(SELECT 1 FROM teams WHERE name = %s) RETURNING id, name", (data.name, team["id"], data.name)).fetchone()
    if row is None:
        raise AppError(409, "TEAM_NAME_TAKEN", f"A team called \"{data.name}\" already exists.")
    return row


def delete_team(conn: Connection, team_id, data: TeamDelete) -> None:
    """Permanently deletes the team with its tasks, history and statuses. The admin
    must type the team's name, so a stray click can't do it."""
    team = _find(conn, "teams", team_id, "Team")
    if data.confirm_name.strip() != team["name"]:
        raise AppError(422, "CONFIRM_MISMATCH", "Type the team's exact name to confirm deleting it.")
    conn.execute("DELETE FROM teams WHERE id = %s", (team["id"],))


def team_members(conn: Connection, team_id) -> list[dict]:
    team = _find(conn, "teams", team_id, "Team")
    return conn.execute(
        """SELECT u.id, u.name, u.email, u.active, m.role FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.team_id = %s ORDER BY (m.role = 'lead') DESC, lower(u.name), u.id""", (team["id"],)).fetchall()


# --------------------------------------------------------------------- users

def _teams_of(conn: Connection, user_ids: list) -> dict:
    out: dict = {}
    if user_ids:
        for r in conn.execute(
                """SELECT m.user_id, t.id AS team_id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id
                   WHERE m.user_id = ANY(%s) ORDER BY lower(t.name)""", (user_ids,)):
            out.setdefault(r["user_id"], []).append({"team_id": r["team_id"], "name": r["name"], "role": r["role"]})
    return out


def _user_dto(row: dict, teams: list) -> dict:
    return {**{k: row[k] for k in ("id", "name", "email", "is_admin", "active", "must_change_password")}, "teams": teams}


def list_users(conn: Connection, q: str | None, page: int, page_size: int) -> dict:
    q = (q or "").strip().lower()
    rows = conn.execute(
        f"""SELECT {_USER_COLUMNS} FROM users
            WHERE %s = '' OR position(%s in lower(name)) > 0 OR position(%s in lower(email)) > 0
            ORDER BY lower(name), id LIMIT %s OFFSET %s""",
        (q, q, q, page_size + 1, (page - 1) * page_size)).fetchall()
    page_rows = rows[:page_size]
    teams = _teams_of(conn, [r["id"] for r in page_rows])
    return {"items": [_user_dto(r, teams.get(r["id"], [])) for r in page_rows], "page": page,
            "has_more": len(rows) > page_size}


def get_user(conn: Connection, user_id) -> dict:
    row = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (_uuid_or_404(user_id, "User"),)).fetchone()
    if row is None:
        raise AppError(404, "NOT_FOUND", "User not found.")
    return _user_dto(row, _teams_of(conn, [row["id"]]).get(row["id"], []))


def _lock_team(conn: Connection, team_id) -> None:
    """Serialise changes to one team's membership so the lead rule can't be raced."""
    if conn.execute("SELECT 1 FROM teams WHERE id = %s FOR UPDATE", (team_id,)).fetchone() is None:
        raise AppError(422, "INVALID_TEAM", "That team doesn't exist.")


def _check_has_lead(conn: Connection, team_id) -> None:
    n = conn.execute("""SELECT count(*) AS members, count(*) FILTER (WHERE role = 'lead') AS leads
                        FROM memberships WHERE team_id = %s""", (team_id,)).fetchone()
    if n["members"] > 0 and n["leads"] == 0:
        name = conn.execute("SELECT name FROM teams WHERE id = %s", (team_id,)).fetchone()["name"]
        raise AppError(409, "TEAM_NEEDS_LEAD",
                       f"\"{name}\" must keep at least one lead. Make someone else a lead first, or add a lead before other members.")


def create_user(conn: Connection, data: UserCreate) -> dict:
    team_ids = [m.team_id for m in data.memberships]
    if len(team_ids) != len(set(team_ids)):
        raise AppError(422, "DUPLICATE_TEAM", "List each team only once.")
    for team_id in sorted(team_ids, key=str):  # fixed order: no deadlocks between concurrent requests
        _lock_team(conn, team_id)
    row = conn.execute(
        f"""INSERT INTO users (email, name, password_hash, is_admin, must_change_password)
            VALUES (%s, %s, %s, %s, true) ON CONFLICT (email) DO NOTHING RETURNING {_USER_COLUMNS}""",
        (data.email, data.name, hash_password(data.temporary_password), data.is_admin)).fetchone()
    if row is None:
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists.")
    for m in data.memberships:
        conn.execute("INSERT INTO memberships (user_id, team_id, role) VALUES (%s, %s, %s)", (row["id"], m.team_id, m.role))
    for team_id in team_ids:
        _check_has_lead(conn, team_id)
    return get_user(conn, row["id"])


def update_user(conn: Connection, actor: dict, user_id, data: UserPatch) -> dict:
    # Lock every active admin first: two admins demoting each other at once can't both succeed.
    conn.execute("SELECT id FROM users WHERE is_admin AND active ORDER BY id FOR UPDATE").fetchall()
    user = _find(conn, "users", user_id, "User")
    changes = {f: getattr(data, f) for f in ("name", "email", "is_admin", "active") if f in data.model_fields_set}
    changes = {f: v for f, v in changes.items() if user[f] != v}
    if not changes:
        return get_user(conn, user["id"])
    if changes.get("active") is False and user["id"] == actor["id"]:
        raise AppError(409, "SELF_DEACTIVATE", "You can't deactivate your own account. Ask another admin.")
    stays_active_admin = changes.get("is_admin", user["is_admin"]) and changes.get("active", user["active"])
    if user["is_admin"] and user["active"] and not stays_active_admin:
        others = conn.execute("SELECT count(*) AS n FROM users WHERE is_admin AND active AND id <> %s",
                              (user["id"],)).fetchone()["n"]
        if others == 0:
            raise AppError(409, "LAST_ADMIN", "There must always be at least one active admin. Make someone else an admin first.")
    sets = ", ".join(f"{f} = %s" for f in changes)  # names come from the fixed tuple above
    row = conn.execute(f"UPDATE users SET {sets} WHERE id = %s AND NOT (%s AND EXISTS "
                       "(SELECT 1 FROM users WHERE email = %s AND id <> %s)) RETURNING id",
                       (*changes.values(), user["id"], "email" in changes, changes.get("email", ""), user["id"])).fetchone()
    if row is None:
        raise AppError(409, "EMAIL_TAKEN", "Another account already uses this email.")
    return get_user(conn, user["id"])


def reset_password(conn: Connection, user_id, data: PasswordReset) -> None:
    user = _find(conn, "users", user_id, "User")
    conn.execute("UPDATE users SET password_hash = %s, must_change_password = true WHERE id = %s",
                 (hash_password(data.temporary_password), user["id"]))


# ---------------------------------------------------------------- membership

def set_membership(conn: Connection, user_id, team_id, role: str) -> dict:
    """Add the user to the team, or change their role in it."""
    user = _find(conn, "users", user_id, "User")
    team_id = _uuid_or_404(team_id, "Team")
    _lock_team(conn, team_id)
    conn.execute("""INSERT INTO memberships (user_id, team_id, role) VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, team_id) DO UPDATE SET role = EXCLUDED.role""", (user["id"], team_id, role))
    _check_has_lead(conn, team_id)
    return get_user(conn, user["id"])


def remove_membership(conn: Connection, actor: dict, user_id, team_id) -> dict:
    """Take the user out of the team. Their tasks there become unassigned, with a history entry each."""
    user = _find(conn, "users", user_id, "User")
    team_id = _uuid_or_404(team_id, "Team")
    _lock_team(conn, team_id)
    gone = conn.execute("DELETE FROM memberships WHERE user_id = %s AND team_id = %s RETURNING role",
                        (user["id"], team_id)).fetchone()
    if gone is None:
        raise AppError(404, "NOT_FOUND", "That user isn't in this team.")
    conn.execute(
        """WITH freed AS (
               UPDATE work_items SET assignee_id = NULL, assigned_by = NULL, version = version + 1,
                      updated_at = now(), last_activity_at = now()
               WHERE team_id = %(team)s AND assignee_id = %(user)s RETURNING id)
           INSERT INTO activity (work_item_id, actor_id, kind, payload)
           SELECT id, %(actor)s, 'assigned',
                  jsonb_build_object('from', %(user)s::text, 'to', NULL, 'from_name', %(name)s::text, 'to_name', NULL)
           FROM freed""", {"team": team_id, "user": user["id"], "actor": actor["id"], "name": user["name"]})
    _check_has_lead(conn, team_id)
    return get_user(conn, user["id"])
