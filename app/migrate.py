"""Bring an existing database up to date without losing data: python -m app.migrate

Safe to run more than once. (app.init_db and app.seed, by contrast, drop and
recreate everything.)
"""
import psycopg

from app.config import DATABASE_URL

STATEMENTS = [
    "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS restricted boolean NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS work_items_created_by_idx ON work_items (created_by)",
    "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS assigned_by uuid REFERENCES users (id) ON DELETE SET NULL",
]

STATUS_TABLE = """
CREATE TABLE statuses (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id  uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    name     text NOT NULL,
    kind     text NOT NULL CHECK (kind IN ('todo', 'in_progress', 'completed', 'custom')),
    color    text NOT NULL DEFAULT 'slate',
    finished boolean NOT NULL DEFAULT false,
    position integer NOT NULL,
    CHECK (kind <> 'completed' OR finished),
    CHECK (kind NOT IN ('todo', 'in_progress') OR NOT finished)
);
CREATE UNIQUE INDEX statuses_team_name_idx ON statuses (team_id, lower(name));
CREATE UNIQUE INDEX statuses_team_kind_idx ON statuses (team_id, kind) WHERE kind <> 'custom';
CREATE INDEX statuses_team_pos_idx ON statuses (team_id, position);
"""


def migrate_statuses(conn: psycopg.Connection) -> bool:
    """Replace the six fixed statuses with per-team statuses. Returns True if it ran.

    Mapping: open -> To do, in_progress -> In progress, done -> Completed,
    awaiting_approval -> To do with approval pending, blocked / cancelled ->
    custom statuses "Blocked" / "Cancelled" (created only for teams that have
    such tasks). Tasks that needed approval and had moved on count as approved.
    """
    already = conn.execute("""SELECT 1 FROM information_schema.columns
                              WHERE table_name = 'work_items' AND column_name = 'status_id'""").fetchone()
    if already:
        return False
    conn.execute(STATUS_TABLE)
    conn.execute("""
        INSERT INTO statuses (team_id, name, kind, color, finished, position)
        SELECT t.id, d.name, d.kind, d.color, d.finished, d.position FROM teams t,
          (VALUES ('To do', 'todo', 'blue', false, 1), ('In progress', 'in_progress', 'violet', false, 2),
                  ('Completed', 'completed', 'green', true, 3)) AS d(name, kind, color, finished, position)""")
    for old, name, color, finished in (("blocked", "Blocked", "red", False), ("cancelled", "Cancelled", "slate", True)):
        conn.execute(f"""
            INSERT INTO statuses (team_id, name, kind, color, finished, position)
            SELECT t.id, %s, 'custom', %s, %s, 3 + (SELECT count(*) FROM statuses s WHERE s.team_id = t.id AND s.kind = 'custom') + 1
            FROM teams t WHERE EXISTS (SELECT 1 FROM work_items w WHERE w.team_id = t.id AND w.status::text = %s)""",
                     (name, color, finished, old))
    conn.execute("""ALTER TABLE work_items ADD COLUMN status_id uuid REFERENCES statuses (id),
                    ADD COLUMN approval text CHECK (approval IN ('pending', 'approved', 'rejected')),
                    ADD COLUMN approval_by uuid REFERENCES users (id) ON DELETE SET NULL""")
    conn.execute("""
        UPDATE work_items w SET status_id = s.id
        FROM statuses s WHERE s.team_id = w.team_id AND (
             (w.status::text IN ('open', 'awaiting_approval') AND s.kind = 'todo')
          OR (w.status::text = 'in_progress' AND s.kind = 'in_progress')
          OR (w.status::text = 'done' AND s.kind = 'completed')
          OR (w.status::text = 'blocked' AND s.kind = 'custom' AND s.name = 'Blocked')
          OR (w.status::text = 'cancelled' AND s.kind = 'custom' AND s.name = 'Cancelled'))""")
    conn.execute("""UPDATE work_items SET approval = CASE WHEN status::text = 'awaiting_approval' THEN 'pending'
                                                         WHEN requires_approval THEN 'approved' END""")
    conn.execute("ALTER TABLE work_items ALTER COLUMN status_id SET NOT NULL")
    conn.execute("DROP INDEX IF EXISTS work_items_team_status_idx")
    conn.execute("DROP INDEX IF EXISTS work_items_assignee_idx")
    conn.execute("ALTER TABLE work_items DROP COLUMN status")
    conn.execute("DROP TYPE IF EXISTS item_status")
    conn.execute("CREATE INDEX work_items_team_status_idx ON work_items (team_id, status_id, priority, last_activity_at DESC)")
    conn.execute("CREATE INDEX work_items_assignee_idx ON work_items (assignee_id, status_id)")
    return True


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        for stmt in STATEMENTS:
            conn.execute(stmt)
        converted = migrate_statuses(conn)
    print(f"Database up to date ({len(STATEMENTS)} statements applied"
          f"{'; statuses converted to per-team statuses' if converted else ''}).")


if __name__ == "__main__":
    main()
