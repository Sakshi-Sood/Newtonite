"""Create or promote an admin from the command line.

    python -m app.create_admin                                   # list users and who is an admin
    python -m app.create_admin --email bob@mail.com              # make an existing user an admin
    python -m app.create_admin --email new@mail.com --name "New Person"   # create an admin (asks for a password)

Needed once on a database that already has users (the first-run setup page only
appears on an empty one), and as a way back in if the last admin is locked out.
"""
import argparse
import getpass
import sys

import psycopg
from pydantic import TypeAdapter, ValidationError

from app.auth import Email, Name, Password, hash_password
from app.config import DATABASE_URL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email")
    parser.add_argument("--name")
    parser.add_argument("--password", help="avoid this on a shared machine; you'll be asked if you leave it out")
    args = parser.parse_args()

    with psycopg.connect(DATABASE_URL) as conn:
        if not args.email:
            rows = conn.execute("SELECT name, email, is_admin, active FROM users ORDER BY is_admin DESC, lower(name)").fetchall()
            if not rows:
                print("No users yet. Open the app in a browser to create the first admin.")
            for name, email, is_admin, active in rows:
                print(f"{'ADMIN' if is_admin else 'user ':5}  {email:35} {name}{'' if active else '  (deactivated)'}")
            print("\nPromote one with:  python -m app.create_admin --email <email>")
            return 0
        try:
            email = TypeAdapter(Email).validate_python(args.email)
        except ValidationError:
            print("That doesn't look like an email address.")
            return 1
        existing = conn.execute("SELECT id, name, is_admin, active FROM users WHERE email = %s", (email,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET is_admin = true, active = true WHERE id = %s", (existing[0],))
            print(f"{existing[1]} <{email}> is now an admin.")
            return 0
        if not args.name:
            print(f"No user with email {email}. To create one, add --name \"Their Name\".")
            return 1
        password = args.password or getpass.getpass("Password for the new admin (8+ characters): ")
        try:
            name = TypeAdapter(Name).validate_python(args.name)
            TypeAdapter(Password).validate_python(password)
        except ValidationError:
            print("Name must be 1-100 characters and the password at least 8.")
            return 1
        conn.execute("INSERT INTO users (email, name, password_hash, is_admin) VALUES (%s, %s, %s, true)",
                     (email, name, hash_password(password)))
        print(f"Created admin {name} <{email}>.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
