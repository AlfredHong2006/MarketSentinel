"""Offline tests for the pre-publication SQLite integrity gate."""

from marketsentinel.storage.sqlite import SQLiteRepository
from scripts.check_database_integrity import main


def test_valid_database_returns_zero(writable_tmp_path) -> None:
    database = writable_tmp_path / "marketsentinel.db"
    SQLiteRepository(database).initialize()

    assert main([str(database)]) == 0


def test_missing_database_returns_two(writable_tmp_path) -> None:
    assert main([str(writable_tmp_path / "missing.db")]) == 2


def test_corrupt_database_returns_one(writable_tmp_path) -> None:
    database = writable_tmp_path / "marketsentinel.db"
    database.write_bytes(b"not a sqlite database")

    assert main([str(database)]) == 1


def test_wrong_argument_count_returns_two() -> None:
    assert main([]) == 2
    assert main(["a", "b"]) == 2
