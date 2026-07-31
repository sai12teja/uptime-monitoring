"""CLI to manage dashboard login accounts (PRD §16 gap 6). No admin UI was
requested, so this is the only way to create the first account.

Run: venv/Scripts/python.exe manage_users.py add <username>
     venv/Scripts/python.exe manage_users.py list
"""
import argparse
import getpass
import sys

import auth
import db


def main():
    parser = argparse.ArgumentParser(description="Manage Rovix dashboard login accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add")
    add_p.add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args()

    if args.command == "add":
        if db.get_user_by_username(args.username):
            print(f"user '{args.username}' already exists", file=sys.stderr)
            sys.exit(1)
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("passwords do not match", file=sys.stderr)
            sys.exit(1)
        db.add_user(args.username, auth.hash_password(password))
        print(f"user '{args.username}' created")
    elif args.command == "list":
        for u in db.list_users():
            print(u["username"])


if __name__ == "__main__":
    main()
