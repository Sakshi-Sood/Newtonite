"""Password hashing, session login, and the current_user dependency."""
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, Request, Response
from psycopg import Connection
from pydantic import BaseModel

from app.db import get_conn
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


def current_user(request: Request, conn: Connection = Depends(get_conn)) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    row = conn.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        request.session.clear()
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    return row


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, conn: Connection = Depends(get_conn)):
    row = conn.execute(
        "SELECT id, email, name, password_hash FROM users WHERE lower(email) = lower(%s)",
        (body.email.strip(),),
    ).fetchone()
    ok = verify_password(body.password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise AppError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    request.session.clear()
    request.session["user_id"] = str(row["id"])
    return {"user": {"id": row["id"], "email": row["email"], "name": row["name"]}}


@router.post("/logout", status_code=204)
def logout(request: Request):
    request.session.clear()
    return Response(status_code=204)


@router.get("/me")
def me(user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    teams = conn.execute(
        """SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id
           WHERE m.user_id = %s ORDER BY t.name""",
        (user["id"],),
    ).fetchall()
    return {"user": user, "teams": teams}


@router.get("/teams/{team_id}/members")
def team_members(team_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    member = conn.execute(
        "SELECT 1 FROM memberships WHERE user_id = %s AND team_id::text = %s", (user["id"], team_id)
    ).fetchone()
    if member is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    return conn.execute(
        """SELECT u.id, u.name, m.role FROM memberships m JOIN users u ON u.id = m.user_id
           WHERE m.team_id::text = %s ORDER BY u.name""",
        (team_id,),
    ).fetchall()
