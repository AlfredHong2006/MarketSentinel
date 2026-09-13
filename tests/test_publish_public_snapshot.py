"""Offline tests for the CI publish script that builds the R2-bound snapshot artifacts.

No network, no LLM calls: everything runs against a locally built SQLite database. Covers the
manifest shape, immutability-by-content-hash, and that a scan failure blocks publication (so the
scheduled workflow never uploads a bad snapshot or overwrites ``latest.json``).
"""

import json
from datetime import UTC, datetime

from conftest import make_article

from marketsentinel.storage.sqlite import SCHEMA_USER_VERSION, SQLiteRepository
from scripts.publish_public_snapshot import publish


def _build_source(writable_tmp_path):
    database = writable_tmp_path / "marketsentinel.db"
    repository = SQLiteRepository(database)
    repository.initialize()
    repository.upsert_articles([make_article()])

    cache = writable_tmp_path / "constituents_cache.json"
    cache.write_text(json.dumps({"source": "test", "constituents": []}), encoding="utf-8")
    return database, cache


def test_publish_writes_versioned_artifacts_and_manifest(writable_tmp_path) -> None:
    database, cache = _build_source(writable_tmp_path)
    output_dir = writable_tmp_path / "dist"

    exit_code = publish(
        database=database,
        constituent_cache=cache,
        output_dir=output_dir,
        public_base_url="https://pub-example.r2.dev/",
        tickers=["NVDA", "PFE"],
        now=datetime(2026, 9, 13, 14, 0, 0, tzinfo=UTC),
    )

    assert exit_code == 0
    version = (output_dir / "version.txt").read_text(encoding="utf-8").strip()
    assert version.startswith("20260913T140000Z-")

    manifest = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version
    assert manifest["schema_user_version"] == SCHEMA_USER_VERSION
    assert manifest["tickers"] == ["NVDA", "PFE"]
    assert (
        manifest["database"]["url"]
        == f"https://pub-example.r2.dev/snapshots/{version}/public-snapshot.db"
    )
    assert manifest["constituents_cache"]["url"] == (
        f"https://pub-example.r2.dev/snapshots/{version}/constituents_cache.json"
    )

    versioned_db = output_dir / version / "public-snapshot.db"
    versioned_cache = output_dir / version / "constituents_cache.json"
    assert versioned_db.is_file()
    assert versioned_cache.is_file()
    assert manifest["database"]["size_bytes"] == versioned_db.stat().st_size
    assert manifest["constituents_cache"]["size_bytes"] == versioned_cache.stat().st_size


def test_publish_is_content_addressed(writable_tmp_path) -> None:
    database, cache = _build_source(writable_tmp_path)

    first = writable_tmp_path / "dist-first"
    second = writable_tmp_path / "dist-second"
    now = datetime(2026, 9, 13, 14, 0, 0, tzinfo=UTC)
    publish(
        database=database,
        constituent_cache=cache,
        output_dir=first,
        public_base_url="https://pub-example.r2.dev",
        tickers=[],
        now=now,
    )
    publish(
        database=database,
        constituent_cache=cache,
        output_dir=second,
        public_base_url="https://pub-example.r2.dev",
        tickers=[],
        now=now,
    )

    assert (first / "version.txt").read_text() == (second / "version.txt").read_text()


def test_publish_refuses_when_source_missing(writable_tmp_path) -> None:
    exit_code = publish(
        database=writable_tmp_path / "missing.db",
        constituent_cache=writable_tmp_path / "missing.json",
        output_dir=writable_tmp_path / "dist",
        public_base_url="https://pub-example.r2.dev",
        tickers=[],
        now=datetime.now(UTC),
    )

    assert exit_code == 2
    assert not (writable_tmp_path / "dist").exists()


def test_publish_blocks_on_secret_scan_failure(writable_tmp_path) -> None:
    database = writable_tmp_path / "marketsentinel.db"
    repository = SQLiteRepository(database)
    repository.initialize()
    tainted = make_article(source="sk-abcdefghijklmnopqrstuvwx1234567890 leaked in source field")
    repository.upsert_articles([tainted])

    cache = writable_tmp_path / "constituents_cache.json"
    cache.write_text(json.dumps({"source": "test", "constituents": []}), encoding="utf-8")
    output_dir = writable_tmp_path / "dist"

    exit_code = publish(
        database=database,
        constituent_cache=cache,
        output_dir=output_dir,
        public_base_url="https://pub-example.r2.dev",
        tickers=[],
        now=datetime.now(UTC),
    )

    assert exit_code == 1
    assert not (output_dir / "latest.json").exists()
    assert not (output_dir / "version.txt").exists()
