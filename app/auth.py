"""Password hashing, session login, and the current_user dependency."""
import hashlib
import hmac
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from psycopg import Connection
from pydantic import BaseModel, StringConstraints, model_validator

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


def current_user(request: Request, conn: Conn) -> dict:
    user_id = request.session.get("user_id")
    if not user_id:
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    row = conn.execute("SELECT id, email, name FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is None:
        request.session.clear()
        raise AppError(401, "UNAUTHENTICATED", "Please log in.")
    return row


User = Annotated[dict, Depends(current_user)]


class LoginIn(BaseModel):
    email: str
    password: str


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=254,
                                         pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class SignupIn(BaseModel):
    name: Name
    email: Email
    password: Annotated[str, StringConstraints(min_length=8, max_length=200)]
    # Exactly one: join an existing team as member, or create a team and lead it.
    team_id: uuid.UUID | None = None
    new_team_name: Name | None = None

    @model_validator(mode="after")
    def _one_team_choice(self):
        if (self.team_id is None) == (self.new_team_name is None):
            raise ValueError("Choose a team to join or name a new team (not both).")
        return self


def signup(conn: Connection, data: SignupIn) -> dict:
    """Create the account and its first team membership in one transaction.

    Unique constraints decide races: two sign-ups with one email (or one new
    team name) can't both succeed, and a failure leaves nothing half-created.
    """
    user = conn.execute(
        """INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s)
           ON CONFLICT (email) DO NOTHING RETURNING id, email, name""",
        (data.email, data.name, hash_password(data.password)),
    ).fetchone()
    if user is None:
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists. Try signing in.")
    if data.new_team_name:
        team = conn.execute(
            "INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
            (data.new_team_name,),
        ).fetchone()
        if team is None:
            raise AppError(409, "TEAM_NAME_TAKEN", "A team with this name already exists. Join it instead.")
        role = "lead"
    else:
        team = conn.execute("SELECT id FROM teams WHERE id = %s", (data.team_id,)).fetchone()
        if team is None:
            raise AppError(422, "INVALID_TEAM", "That team no longer exists.")
        role = "member"
    conn.execute("INSERT INTO memberships (user_id, team_id, role) VALUES (%s, %s, %s)",
                 (user["id"], team["id"], role))
    return user


@router.post("/signup", status_code=201)
def signup_route(body: SignupIn, request: Request, conn: Conn):
    user = signup(conn, body)
    request.session.clear()
    request.session["user_id"] = str(user["id"])
    return {"user": user}


@router.get("/teams")
def list_teams(conn: Conn):
    """Team names for the sign-up form (no login needed)."""
    return conn.execute("SELECT id, name FROM teams ORDER BY name").fetchall()


@router.post("/login")
def login(body: LoginIn, request: Request, conn: Conn):
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
def me(user: User, conn: Conn):
    teams = conn.execute(
        """SELECT t.id, t.name, m.role FROM memberships m JOIN teams t ON t.id = m.team_id
           WHERE m.user_id = %s ORDER BY t.name""",
        (user["id"],),
    ).fetchall()
    return {"user": user, "teams": teams}


@router.get("/teams/{team_id}/members")
def team_members(team_id: str, user: User, conn: Conn):
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
