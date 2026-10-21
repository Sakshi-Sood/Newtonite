"""Admin dashboard endpoints. Every route requires an admin (checked on the server)."""
from fastapi import APIRouter, Query, Response

from app import admin, projects
from app.auth import Admin
from app.db import Conn

router = APIRouter(prefix="/api/admin")


@router.get("/summary")
def summary(_: Admin, conn: Conn):
    return admin.summary(conn)


# ---- teams (fixed paths are declared before /{team_id}) ----
@router.get("/teams/options")
def team_options(_: Admin, conn: Conn):
    return admin.team_options(conn)


@router.get("/teams")
def list_teams(_: Admin, conn: Conn, q: str | None = None, page: int = Query(1, ge=1, le=10000),
               page_size: int = Query(50, ge=1, le=100)):
    return admin.list_teams(conn, q, page, page_size)


@router.post("/teams", status_code=201)
def create_team(body: admin.TeamIn, _: Admin, conn: Conn):
    return admin.create_team(conn, body)


@router.patch("/teams/{team_id}")
def rename_team(team_id: str, body: admin.TeamIn, _: Admin, conn: Conn):
    return admin.rename_team(conn, team_id, body)


@router.delete("/teams/{team_id}", status_code=204)
def delete_team(team_id: str, body: admin.TeamDelete, _: Admin, conn: Conn):
    admin.delete_team(conn, team_id, body)
    return Response(status_code=204)


@router.get("/teams/{team_id}/members")
def team_members(team_id: str, _: Admin, conn: Conn):
    return admin.team_members(conn, team_id)


# ---- projects (fixed paths first; every route is admin only) ----
@router.get("/projects")
def list_projects(_: Admin, conn: Conn, q: str | None = None, page: int = Query(1, ge=1, le=10000),
                  page_size: int = Query(50, ge=1, le=100)):
    return projects.list_projects(conn, q, page, page_size)


@router.post("/projects", status_code=201)
def create_project(body: projects.ProjectIn, _: Admin, conn: Conn):
    return projects.create_project(conn, body)


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: projects.ProjectPatch, _: Admin, conn: Conn):
    return projects.update_project(conn, project_id, body)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, body: projects.ProjectDelete, actor: Admin, conn: Conn):
    projects.delete_project(conn, actor["id"], project_id, body)
    return Response(status_code=204)


@router.get("/projects/{project_id}/teams")
def project_teams(project_id: str, _: Admin, conn: Conn):
    return projects.project_teams(conn, project_id)


@router.put("/projects/{project_id}/teams/{team_id}")
def add_project_team(project_id: str, team_id: str, _: Admin, conn: Conn):
    return projects.add_team(conn, project_id, team_id)


@router.delete("/projects/{project_id}/teams/{team_id}")
def remove_project_team(project_id: str, team_id: str, actor: Admin, conn: Conn):
    return projects.remove_team(conn, actor["id"], project_id, team_id)


# ---- users ----
@router.get("/users")
def list_users(_: Admin, conn: Conn, q: str | None = None, page: int = Query(1, ge=1, le=10000),
               page_size: int = Query(25, ge=1, le=100)):
    return admin.list_users(conn, q, page, page_size)


@router.post("/users", status_code=201)
def create_user(body: admin.UserCreate, _: Admin, conn: Conn):
    return admin.create_user(conn, body)


@router.get("/users/{user_id}")
def get_user(user_id: str, _: Admin, conn: Conn):
    return admin.get_user(conn, user_id)


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: admin.UserPatch, actor: Admin, conn: Conn):
    return admin.update_user(conn, actor, user_id, body)


@router.post("/users/{user_id}/password", status_code=204)
def reset_password(user_id: str, body: admin.PasswordReset, _: Admin, conn: Conn):
    admin.reset_password(conn, user_id, body)
    return Response(status_code=204)


# ---- membership ----
@router.put("/users/{user_id}/teams/{team_id}")
def set_membership(user_id: str, team_id: str, body: admin.RoleIn, _: Admin, conn: Conn):
    return admin.set_membership(conn, user_id, team_id, body.role)


@router.delete("/users/{user_id}/teams/{team_id}")
def remove_membership(user_id: str, team_id: str, actor: Admin, conn: Conn):
    return admin.remove_membership(conn, actor, user_id, team_id)
