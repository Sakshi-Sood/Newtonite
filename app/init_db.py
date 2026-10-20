"""Create (or recreate) the schema: python -m app.init_db

Drops all application tables first, so any existing data is lost.
"""
import psycopg

from app.config import DATABASE_URL
from app.db import apply_schema


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        apply_schema(conn)
    print("Schema applied.")


if __name__ == "__main__":
    main()
