"""Projects: made and managed by admins, assigned to any number of teams.

A task can be tagged with one project, and only one of its own team's projects.
Rules kept here:
- taking a team off a project, or deleting the project, releases that team's tasks
  from it: open tasks move to the team's Unassigned status, the rest are only
  untagged; each gets history entries and nothing is ever deleted;
- tasks awaiting approval or already closed (finished, or rejected by a lead) are
  only untagged: their status is locked or final, so we don't move them.
"""
import uuid
from typing import Annotated

from psycopg import Connection
from pydantic import BaseModel, StringConstraints, model_validator

from app.errors import AppError
from app.notify import publish

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class ProjectIn(BaseModel):
    name: ProjectName
    description: Description = ""


class ProjectPatch(BaseModel):
    name: ProjectName | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class ProjectDelete(BaseModel):
    confirm_name: str


def _uuid_or_404(value, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise AppError(404, "NOT_FOUND", f"{what} not found.")


def _find(conn: Connection, project_id) -> dict:
    row = conn.execute("SELECT id, name, description FROM projects WHERE id = %s",
                       (_uuid_or_404(project_id, "Project"),)).fetchone()
    if row is None:
        raise AppError(404, "NOT_FOUND", "Project not found.")
    return row


# ------------------------------------------------------------- admin: manage

def list_projects(conn: Connection, q: str | None, page: int, page_size: int) -> dict:
    q = (q or "").strip().lower()
    rows = conn.execute(
        """SELECT p.id, p.name, p.description,
                  (SELECT count(*) FROM project_teams pt WHERE pt.project_id = p.id) AS team_count,
                  (SELECT coalesce(jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name) ORDER BY lower(t.name)), '[]')
                     FROM project_teams pt JOIN teams t ON t.id = pt.team_id WHERE pt.project_id = p.id) AS teams,
                  (SELECT count(*) FROM work_items w WHERE w.project_id = p.id) AS total_tasks,
                  (SELECT count(*) FROM work_items w JOIN statuses s ON s.id = w.status_id
                    WHERE w.project_id = p.id AND NOT s.finished AND w.approval IS DISTINCT FROM 'rejected') AS open_tasks
           FROM projects p
           WHERE %s = '' OR position(%s in lower(p.name)) > 0
           ORDER BY lower(p.name), p.id LIMIT %s OFFSET %s""",
        (q, q, page_size + 1, (page - 1) * page_size)).fetchall()
    return {"items": rows[:page_size], "page": page, "has_more": len(rows) > page_size}


def create_project(conn: Connection, data: ProjectIn) -> dict:
    row = conn.execute(
        """INSERT INTO projects (name, description) VALUES (%s, %s)
           ON CONFLICT (lower(name)) DO NOTHING RETURNING id, name, description""",
        (data.name, data.description)).fetchone()
    if row is None:
        raise AppError(409, "PROJECT_NAME_TAKEN", f"A project called \"{data.name}\" already exists.")
    return {**row, "team_count": 0, "teams": [], "total_tasks": 0, "open_tasks": 0}


def update_project(conn: Connection, project_id, data: ProjectPatch) -> dict:
    project = _find(conn, project_id)
    changes = {f: getattr(data, f) for f in ("name", "description") if f in data.model_fields_set}
    changes = {f: v for f, v in changes.items() if project[f] != v}
    if not changes:
        return project
    taken = "name" in changes and conn.execute(
        "SELECT 1 FROM projects WHERE lower(name) = lower(%s) AND id <> %s", (changes["name"], project["id"])).fetchone()
    if taken:
        raise AppError(409, "PROJECT_NAME_TAKEN", f"A project called \"{changes['name']}\" already exists.")
    sets = ", ".join(f"{f} = %s" for f in changes)  # names come from the fixed tuple above
    return conn.execute(f"UPDATE projects SET {sets} WHERE id = %s RETURNING id, name, description",
                        (*changes.values(), project["id"])).fetchone()


def project_teams(conn: Connection, project_id) -> list[dict]:
    """Every team, with whether the project is on it and how many tasks of that team carry the project."""
    project = _find(conn, project_id)
    return conn.execute(
        """SELECT t.id, t.name,
                  EXISTS (SELECT 1 FROM project_teams pt WHERE pt.project_id = %(p)s AND pt.team_id = t.id) AS assigned,
                  (SELECT count(*) FROM work_items w WHERE w.project_id = %(p)s AND w.team_id = t.id) AS task_count
           FROM teams t ORDER BY lower(t.name), t.id""", {"p": project["id"]}).fetchall()


def add_team(conn: Connection, project_id, team_id) -> dict:
    project = _find(conn, project_id)
    team_id = _uuid_or_404(team_id, "Team")
    if conn.execute("SELECT 1 FROM teams WHERE id = %s", (team_id,)).fetchone() is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    conn.execute("INSERT INTO project_teams (project_id, team_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (project["id"], team_id))
    return {"project_id": project["id"], "team_id": team_id, "assigned": True}


def remove_team(conn: Connection, actor_id, project_id, team_id) -> dict:
    """Take the team off the project; its tasks are released (see module docstring)."""
    project = _find(conn, project_id)
    team_id = _uuid_or_404(team_id, "Team")
    # Deleting the row first makes a concurrent tagger (who holds it FOR SHARE) finish before we release.
    conn.execute("DELETE FROM project_teams WHERE project_id = %s AND team_id = %s", (project["id"], team_id))
    return {"project_id": project["id"], "team_id": team_id, "assigned": False,
            "released": _release(conn, actor_id, project["id"], team_id)}


def delete_project(conn: Connection, actor_id, project_id, data: ProjectDelete) -> None:
    """The admin types the project's name to confirm. Its tasks are released first."""
    project = _find(conn, project_id)
    if data.confirm_name.strip() != project["name"]:
        raise AppError(422, "CONFIRM_MISMATCH", "Type the project's exact name to confirm deleting it.")
    conn.execute("DELETE FROM project_teams WHERE project_id = %s", (project["id"],))
    _release(conn, actor_id, project["id"])
    conn.execute("DELETE FROM projects WHERE id = %s", (project["id"],))


_RELEASE_SQL = """
WITH hit AS (
    SELECT w.id, w.team_id, w.status_id, s.name AS status_name, p.name AS project_name,
           (NOT s.finished AND s.kind <> 'unassigned'
            AND w.approval IS DISTINCT FROM 'pending' AND w.approval IS DISTINCT FROM 'rejected') AS movable
    FROM work_items w
    JOIN statuses s ON s.id = w.status_id
    JOIN projects p ON p.id = w.project_id
    WHERE w.project_id = %(project)s AND (%(team)s::uuid IS NULL OR w.team_id = %(team)s::uuid)
    FOR UPDATE OF w
), plan AS (
    SELECT h.*, u.id AS to_id, u.name AS to_name, u.color AS to_color
    FROM hit h LEFT JOIN statuses u ON u.team_id = h.team_id AND u.kind = 'unassigned' AND h.movable
), untag_log AS (
    INSERT INTO activity (work_item_id, actor_id, kind, payload)
    SELECT id, %(actor)s, 'updated',
           jsonb_build_object('changes', jsonb_build_object('project',
                              jsonb_build_object('from', project_name, 'to', NULL)))
    FROM plan
), move_log AS (
    INSERT INTO activity (work_item_id, actor_id, kind, payload)
    SELECT id, %(actor)s, 'status_changed',
           jsonb_build_object('from', status_name, 'to', to_name, 'from_id', status_id::text,
                              'to_id', to_id::text, 'to_color', to_color, 'reason', 'Project changed')
    FROM plan WHERE to_id IS NOT NULL
), changed AS (
    UPDATE work_items w
    SET project_id = NULL, status_id = COALESCE(plan.to_id, w.status_id),
        version = w.version + 1, updated_at = now(), last_activity_at = now()
    FROM plan WHERE w.id = plan.id
)
SELECT team_id, count(*) AS n FROM plan GROUP BY team_id
"""


def _release(conn: Connection, actor_id, project_id, team_id=None) -> int:
    """Untag the project's tasks (of one team, or of all) and move the open ones to
    Unassigned, with history. Returns how many tasks were released."""
    rows = conn.execute(_RELEASE_SQL, {"project": project_id, "team": team_id, "actor": actor_id}).fetchall()
    if rows:  # tabs showing these teams' boards hear that columns changed
        publish(conn, {"kind": "statuses", "team_ids": [str(r["team_id"]) for r in rows], "actor_id": str(actor_id)})
    return sum(r["n"] for r in rows)


# ------------------------------------------------------------ tagging tasks

def require_taggable(conn: Connection, team_id, project_id) -> None:
    """The project must be assigned to the task's team (422 otherwise). The row stays
    locked until commit, so taking the team off the project can't slip between check and write."""
    ok = conn.execute("SELECT 1 FROM project_teams WHERE project_id = %s AND team_id = %s FOR SHARE",
                      (project_id, team_id)).fetchone()
    if ok is None:
        raise AppError(422, "INVALID_PROJECT", "That project isn't assigned to the task's team.")


def is_on_team(conn: Connection, team_id, project_id) -> bool:
    return conn.execute("SELECT 1 FROM project_teams WHERE project_id = %s AND team_id = %s",
                        (project_id, team_id)).fetchone() is not None


def name_of(conn: Connection, project_id) -> str | None:
    if project_id is None:
        return None
    row = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
    return row["name"] if row else None


# --------------------------------------------------------------- everyone

def for_user(conn: Connection, user_id) -> dict[str, list[dict]]:
    """Projects of every team the user is in, keyed by team id (for /api/me)."""
    rows = conn.execute(
        """SELECT pt.team_id, p.id, p.name FROM project_teams pt
           JOIN projects p ON p.id = pt.project_id
           JOIN memberships m ON m.team_id = pt.team_id AND m.user_id = %s
           ORDER BY lower(p.name), p.id""", (user_id,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r["team_id"]), []).append({"id": r["id"], "name": r["name"]})
    return out


def list_visible(conn: Connection, user_id, team_id: str | None) -> list[dict]:
    """Projects for pickers and filters: one team's (any signed-in user may send a task
    to any team), or, without a team, those of the caller's own teams."""
    if team_id:
        return conn.execute(
            """SELECT p.id, p.name FROM project_teams pt JOIN projects p ON p.id = pt.project_id
               WHERE pt.team_id = %s ORDER BY lower(p.name), p.id""",
            (_uuid_or_404(team_id, "Team"),)).fetchall()
    return conn.execute(
        """SELECT DISTINCT p.id, p.name FROM projects p
           JOIN project_teams pt ON pt.project_id = p.id
           JOIN memberships m ON m.team_id = pt.team_id AND m.user_id = %s
           ORDER BY p.name, p.id""", (user_id,)).fetchall()
