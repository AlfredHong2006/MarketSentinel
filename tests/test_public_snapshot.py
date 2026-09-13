"""Offline tests for the public deployment's startup snapshot fetch.

No network: every HTTP call goes through ``httpx.MockTransport``. The central guarantee under
test is the fallback contract -- any failure (unconfigured, unreachable, wrong hash, corrupt
database, mismatched schema) must leave the target database and cache files byte-for-byte
untouched, exactly as ``fetch_public_snapshot`` promises never to raise.
"""

import hashlib
import json

import httpx
import pytest

from marketsentinel.config import Settings
from marketsentinel.public_snapshot import fetch_public_snapshot
from marketsentinel.storage.sqlite import SCHEMA_USER_VERSION, SQLiteRepository

MANIFEST_URL = "https://snapshots.test/latest.json"
DATABASE_URL = "https://snapshots.test/snapshots/v1/public-snapshot.db"
CACHE_URL = "https://snapshots.test/snapshots/v1/constituents_cache.json"


def _valid_database_bytes(tmp_path) -> bytes:
    path = tmp_path / "source.db"
    SQLiteRepository(path).initialize()
    return path.read_bytes()


def _settings(writable_tmp_path, *, manifest_url: str | None) -> Settings:
    baked_database = writable_tmp_path / "marketsentinel.db"
    baked_cache = writable_tmp_path / "constituents_cache.json"
    baked_database.write_bytes(b"baked-in database bytes")
    baked_cache.write_text('{"source": "baked-in"}', encoding="utf-8")
    return Settings(
        database_path=baked_database,
        constituent_cache_path=baked_cache,
        public_snapshot_manifest_url=manifest_url,
        public_snapshot_fetch_timeout_seconds=5,
    )


def _manifest(
    db_bytes: bytes, cache_bytes: bytes, *, schema_user_version: int = SCHEMA_USER_VERSION
) -> dict:
    return {
        "version": "v1",
        "published_at": "2026-09-13T12:00:00+00:00",
        "schema_user_version": schema_user_version,
        "tickers": ["NVDA"],
        "database": {
            "url": DATABASE_URL,
            "sha256": hashlib.sha256(db_bytes).hexdigest(),
            "size_bytes": len(db_bytes),
        },
        "constituents_cache": {
            "url": CACHE_URL,
            "sha256": hashlib.sha256(cache_bytes).hexdigest(),
            "size_bytes": len(cache_bytes),
        },
    }


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _served(manifest: dict, db_bytes: bytes, cache_bytes: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(200, json=manifest)
        if str(request.url) == DATABASE_URL:
            return httpx.Response(200, content=db_bytes)
        if str(request.url) == CACHE_URL:
            return httpx.Response(200, content=cache_bytes)
        return httpx.Response(404)

    return handler


def test_unconfigured_manifest_url_is_a_no_op(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=None)
    before = settings.database_path.read_bytes()

    outcome = fetch_public_snapshot(settings)

    assert outcome.applied is False
    assert outcome.reason == "not configured"
    assert settings.database_path.read_bytes() == before


def test_successful_fetch_replaces_database_and_cache(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    db_bytes = _valid_database_bytes(writable_tmp_path)
    cache_bytes = json.dumps({"source": "published"}).encode("utf-8")
    manifest = _manifest(db_bytes, cache_bytes)

    with _client(_served(manifest, db_bytes, cache_bytes)) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is True
    assert outcome.version == "v1"
    assert settings.database_path.read_bytes() == db_bytes
    assert json.loads(settings.constituent_cache_path.read_text()) == {"source": "published"}


def test_schema_mismatch_leaves_baked_in_snapshot_untouched(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()
    before_cache = settings.constituent_cache_path.read_bytes()
    db_bytes = _valid_database_bytes(writable_tmp_path)
    cache_bytes = b'{"source": "published"}'
    manifest = _manifest(db_bytes, cache_bytes, schema_user_version=SCHEMA_USER_VERSION + 1)

    with _client(_served(manifest, db_bytes, cache_bytes)) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert "schema mismatch" in outcome.reason
    assert settings.database_path.read_bytes() == before_db
    assert settings.constituent_cache_path.read_bytes() == before_cache


def test_sha256_mismatch_leaves_baked_in_snapshot_untouched(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()
    db_bytes = _valid_database_bytes(writable_tmp_path)
    cache_bytes = b'{"source": "published"}'
    manifest = _manifest(db_bytes, cache_bytes)
    manifest["database"]["sha256"] = "0" * 64  # deliberately wrong

    with _client(_served(manifest, db_bytes, cache_bytes)) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert "sha256 mismatch" in outcome.reason
    assert settings.database_path.read_bytes() == before_db


def test_corrupt_database_fails_integrity_check_and_is_rejected(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()
    db_bytes = b"not actually a sqlite database"
    cache_bytes = b'{"source": "published"}'
    manifest = _manifest(db_bytes, cache_bytes)  # hash matches, content is still garbage

    with _client(_served(manifest, db_bytes, cache_bytes)) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert settings.database_path.read_bytes() == before_db


def test_manifest_unreachable_leaves_baked_in_snapshot_untouched(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    with _client(handler) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert settings.database_path.read_bytes() == before_db


def test_malformed_manifest_json_leaves_baked_in_snapshot_untouched(writable_tmp_path) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with _client(handler) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert settings.database_path.read_bytes() == before_db


@pytest.mark.parametrize("status_code", [404, 500])
def test_asset_http_error_leaves_baked_in_snapshot_untouched(
    writable_tmp_path, status_code
) -> None:
    settings = _settings(writable_tmp_path, manifest_url=MANIFEST_URL)
    before_db = settings.database_path.read_bytes()
    db_bytes = _valid_database_bytes(writable_tmp_path)
    cache_bytes = b'{"source": "published"}'
    manifest = _manifest(db_bytes, cache_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(200, json=manifest)
        return httpx.Response(status_code)

    with _client(handler) as client:
        outcome = fetch_public_snapshot(settings, client=client)

    assert outcome.applied is False
    assert settings.database_path.read_bytes() == before_db
