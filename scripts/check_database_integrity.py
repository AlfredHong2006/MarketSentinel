"""Fail loudly if a SQLite database does not pass its own consistency check.

A focused pre-publication gate for the scheduled coverage workflow: run against the private
operational database right after a coverage cycle, before any snapshot is built or uploaded, so a
corrupt database never reaches the sanitisation step or R2.

Usage:
    uv run python scripts/check_database_integrity.py data/marketsentinel.db
"""

import sqlite3
import sys
from pathlib import Path

from marketsentinel.storage.sqlite import integrity_check


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: check_database_integrity.py <path>", file=sys.stderr)
        return 2

    path = Path(arguments[0])
    if not path.is_file():
        print(f"database not found: {path}", file=sys.stderr)
        return 2

    try:
        result = integrity_check(path)
    except sqlite3.DatabaseError as error:
        # Not even a valid SQLite file: PRAGMA integrity_check itself cannot run.
        print(f"{path}: not a valid SQLite database ({error})", file=sys.stderr)
        return 1

    print(f"{path}: integrity_check = {result}")
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
