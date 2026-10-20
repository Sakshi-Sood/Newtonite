"""Password hashing, session login, first-run setup, and the user dependencies.

There is no public sign-up: accounts are created by an admin (app.admin), except
the very first admin, who is created once on an empty database (/api/setup).
"""
import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from psycopg import Connection
from pydantic import BaseModel, StringConstraints

from app import statuses
from app.db import Conn
from app.errors import AppError

router = APIRouter(prefix="/api")

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), hash_hex)


# Used when the email is unknown, so a wrong email costs the same time as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
Password = Annotated[str, StringConstraints(min_length=8, max_length=200)]

_USER_COLUMNS = "id, email, name, is_admin, active, must_change_password"


def session_user(request: Request, conn: Conn) -> dict:
    """The signed-in user, re-read from the database on every request, so a
    deactivated account is locked out at once. Does not enforce the
    change-your-temporary-password step (see current_user)."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    row = conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None or not row["active"]:
        request.session.clear()
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    return row


def current_user(user: Annotated[dict, Depends(session_user)]) -> dict:
    """For everything except changing the password: a user holding a temporary
    password must choose their own before doing anything else."""
    if user["must_change_password"]:
        raise AppError(403, "PASSWORD_CHANGE_REQUIRED", "Choose your own password before continuing.")
    return user


def require_admin(user: Annotated[dict, Depends(current_user)]) -> dict:
    if not user["is_admin"]:
        raise AppError(403, "FORBIDDEN", "Only admins can do this.", {"permission": "admin"})
    return user


User = Annotated[dict, Depends(current_user)]
SessionUser = Annotated[dict, Depends(session_user)]
Admin = Annotated[dict, Depends(require_admin)]


def public_user(row: dict) -> dict:
    return {k: row[k] for k in ("id", "email", "name", "is_admin", "must_change_password")}


class LoginIn(BaseModel):
    email: str
    password: str


class SetupIn(BaseModel):
    name: Name
    email: Email
    password: Password


def setup_needed(conn: Connection) -> bool:
    return conn.execute("SELECT NOT EXISTS (SELECT 1 FROM users) AS needed").fetchone()["needed"]


def create_first_admin(conn: Connection, data: SetupIn) -> dict:
    """One-time setup: allowed only while the database has no users at all."""
    conn.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")  # two simultaneous setups can't both pass
    if not setup_needed(conn):
        raise AppError(409, "SETUP_DONE", "Setup is already complete. Ask an admin for an account.")
    return conn.execute(
        f"""INSERT INTO users (email, name, password_hash, is_admin) VALUES (%s, %s, %s, true)
            RETURNING {_USER_COLUMNS}""",
        (data.email, data.name, hash_password(data.password))).fetchone()


@router.get("/setup")
def setup_status(conn: Conn):
    """Does this installation still need its first admin? (No login needed.)"""
    return {"needed": setup_needed(conn)}


@router.post("/setup", status_code=201)
def setup(body: SetupIn, request: Request, conn: Conn):
    user = create_first_admin(conn, body)
    request.session.clear()
    request.session["user_id"] = str(user["id"])
    return {"user": public_user(user)}


@router.get("/teams")
def list_teams(user: User, conn: Conn):
    """Team names, for choosing where to send a task."""
    return conn.execute("SELECT id, name FROM teams ORDER BY name").fetchall()


@router.post("/login")
def login(body: LoginIn, request: Request, conn: Conn):
    row = conn.execute(
        f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE lower(email) = lower(%s)",
        (body.email.strip(),),
    ).fetchone()
    ok = verify_password(body.password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise AppError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    if not row["active"]:  # only said to someone who knows the password
        raise AppError(403, "ACCOUNT_DISABLED", "This account has been deactivated. Ask an admin to reactivate it.")
    request.session.clear()
    request.session["user_id"] = str(row["id"])
    return {"user": public_user(row)}


@router.post("/logout", status_code=204)
def logout(request: Request):
    request.session.clear()
    return Response(status_code=204)


@router.get("/me")
def me(user: SessionUser, conn: Conn):
    teams = conn.execute(
        """SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id
           WHERE m.user_id = %s ORDER BY t.name""",
        (user["id"],),
    ).fetchall()
    by_team = statuses.for_user(conn, user["id"])
    return {"user": public_user(user), "teams": [{**t, "statuses": by_team.get(str(t["id"]), [])} for t in teams]}


@router.get("/teams/{team_id}/members")
def team_members(team_id: str, user: User, conn: Conn):
    member = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id = %s AND team_id::text = %s", (user["id"], team_id)
    ).fetchone()
    if member is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    return conn.execute(
        """SELECT u.id, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.team_id::text = %s AND u.active ORDER BY u.name""",
        (team_id,),
    ).fetchall()
