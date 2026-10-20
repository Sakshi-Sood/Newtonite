"""Bring an existing database up to date without losing data: python -m app.migrate

Every statement is idempotent, so it is safe to run more than once.
(app.init_db and app.seed, by contrast, drop and recreate everything.)
"""
import psycopg

from app.config import DATABASE_URL

STATEMENTS = [
    "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS restricted boolean NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS work_items_created_by_idx ON work_items (created_by)",
    "ALTER TABLE work_items ADD COLUMN IF NOT EXISTS assigned_by uuid REFERENCES users (id) ON DELETE SET NULL",
]


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        for stmt in STATEMENTS:
            conn.execute(stmt)
    print(f"Database up to date ({len(STATEMENTS)} statements applied).")


if __name__ == "__main__":
    main()
