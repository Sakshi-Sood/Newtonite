"""Demo data: python -m app.seed --items 20000

Recreates the schema (ALL existing data is deleted), then creates teams,
users and work items with a realistic mix of states, ages and due dates.
Every account's password is "password".
"""
import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from app.auth import hash_password
from app.config import DATABASE_URL
from app.db import apply_schema

TEAMS = ["Payments", "Platform", "Support", "Compliance", "Customer Success", "Infrastructure"]

# email, name, {team: role}
DEMO_USERS = [
    ("lead@newtonite.test", "Lena Lead", {"Payments": "lead", "Platform": "lead"}),
    ("lead2@newtonite.test", "Omar Second", {"Payments": "lead"}),
    ("member@newtonite.test", "Maya Member", {"Payments": "member", "Platform": "member"}),
    ("outsider@newtonite.test", "Otto Outsider", {"Support": "member"}),
]
FIRST = ["Ava", "Ben", "Chen", "Dara", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo", "Kai", "Lior",
         "Mina", "Nils", "Ola", "Priya", "Quinn", "Ravi", "Sara", "Tomas", "Uma", "Vik", "Wen", "Yusuf"]
LAST = ["Andersen", "Bauer", "Costa", "Diaz", "Evans", "Fischer", "Garcia", "Haddad", "Ito", "Jensen",
        "Khan", "Lopez", "Moreau", "Nakamura", "Okafor", "Petrov", "Rossi", "Silva", "Tanaka", "Weber"]

TITLES = {
    "customer_issue": ["Customer cannot log in after password reset", "Invoice shows wrong currency",
                       "Customer reports missing order confirmation", "Account locked after MFA change"],
    "engineering": ["Search API returns 500 for long queries", "Upgrade database driver",
                    "Flaky deploy step in build pipeline", "Memory leak in report exporter"],
    "payment": ["Refund failed for order", "Duplicate charge reported by customer",
                "Payout delayed to merchant", "Card payment stuck in pending"],
    "incident": ["Checkout latency above SLO", "Login service returning errors",
                 "Webhook deliveries failing", "Background queue backing up"],
    "compliance": ["GDPR data export request", "Quarterly access review",
                   "Retention policy exception request", "Audit evidence for SOC 2 control"],
    "task": ["Rotate API credentials", "Update on-call rota", "Approve vendor contract renewal",
             "Prepare monthly ops report"],
}
STATUS_WEIGHTS = {"awaiting_approval": 6, "open": 22, "in_progress": 24, "blocked": 6, "done": 34, "cancelled": 8}
PRIORITY_WEIGHTS = {"urgent": 6, "high": 20, "medium": 50, "low": 24}


def pick(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=20000)
    parser.add_argument("--users", type=int, default=40)
    args = parser.parse_args()
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    pw = hash_password("password")  # one hash reused: demo data only

    with psycopg.connect(DATABASE_URL) as conn:
        apply_schema(conn)
        team_ids = {name: conn.execute("INSERT INTO teams (name) VALUES (%s) RETURNING id", (name,)).fetchone()[0]
                    for name in TEAMS}
        members: dict[str, list[tuple[uuid.UUID, str]]] = {name: [] for name in TEAMS}

        def add_user(email: str, name: str, teams: dict[str, str]) -> None:
            uid = conn.execute("INSERT INTO users (email, name, password_hash) VALUES (%s, %s, %s) RETURNING id",
                               (email, name, pw)).fetchone()[0]
            for team, role in teams.items():
                conn.execute("INSERT INTO memberships VALUES (%s, %s, %s)", (uid, team_ids[team], role))
                members[team].append((uid, role))

        for email, name, teams in DEMO_USERS:
            add_user(email, name, teams)
        for i in range(args.users - len(DEMO_USERS)):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            teams = {t: ("lead" if rng.random() < 0.15 else "member") for t in rng.sample(TEAMS, rng.choice([1, 1, 2]))}
            add_user(f"user{i + 1}@newtonite.test", name, teams)
        for team in TEAMS:  # every team needs a lead to approve things
            if members[team] and not any(role == "lead" for _, role in members[team]):
                uid, _ = members[team][0]
                conn.execute("UPDATE memberships SET role = 'lead' WHERE user_id = %s AND team_id = %s",
                             (uid, team_ids[team]))
                members[team][0] = (uid, "lead")

        item_rows, activity_rows = [], []
        for n in range(args.items):
            team = rng.choice(TEAMS)
            people = [uid for uid, _ in members[team]]
            item_type = rng.choice(list(TITLES))
            status = pick(rng, STATUS_WEIGHTS)
            created_at = now - timedelta(days=rng.uniform(0, 120))
            last_activity = created_at + (now - created_at) * rng.random() ** 2
            needs_assignee = status in ("in_progress", "blocked", "done") or (status == "open" and rng.random() < 0.4)
            due_at = now + timedelta(days=rng.uniform(-10, 25)) if rng.random() < 0.3 else None
            item_id = uuid.uuid4()
            item_rows.append((
                item_id, team_ids[team], f"{rng.choice(TITLES[item_type])} #{1000 + n}",
                f"Reported via the {team} queue. Investigate, act, and record the outcome here.",
                item_type, pick(rng, PRIORITY_WEIGHTS), status,
                rng.choice(people) if needs_assignee else None,
                status == "awaiting_approval" or rng.random() < 0.1, due_at, rng.choice(people),
                created_at, last_activity, last_activity,
            ))
            activity_rows.append((item_id, item_rows[-1][10], "created", Jsonb({"status": status}), created_at))

        with conn.cursor() as cur:
            for i in range(0, len(item_rows), 1000):
                cur.executemany(
                    """INSERT INTO work_items (id, team_id, title, description, type, priority, status,
                           assignee_id, requires_approval, due_at, created_by, created_at, updated_at,
                           last_activity_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    item_rows[i:i + 1000],
                )
                cur.executemany(
                    "INSERT INTO activity (work_item_id, actor_id, kind, payload, created_at) VALUES (%s, %s, %s, %s, %s)",
                    activity_rows[i:i + 1000],
                )
        conn.execute("ANALYZE")
    print(f"Seeded {len(TEAMS)} teams, {args.users} users, {args.items} work items.")
    print("Log in as lead@ / lead2@ / member@ / outsider@newtonite.test with password 'password'.")


if __name__ == "__main__":
    main()
