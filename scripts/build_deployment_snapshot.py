"""Build the frozen deployment artifacts the public deployment ships.

Produces ``deploy/public-snapshot.db`` and ``deploy/constituents_cache.json`` from the live
local runtime data, then scans both for secrets and personal data before they are considered
ready to commit.

Two things this deliberately does *not* do:

- It never copies the live database file byte-for-byte. A plain copy of a WAL-mode database can
  omit everything still sitting in ``-wal``, producing a snapshot that is silently missing the
  most recent analyses. ``VACUUM INTO`` takes a transactionally consistent, compacted copy
  through SQLite itself, which is the only correct way to snapshot a live SQLite database.
- It never writes to the live database. The source is opened read-only.

The constituent cache is copied because the read path resolves companies from the cache only
(``MarketAnalysisService.read_stored`` calls ``resolve_cached``). Without it, every company
overview 404s and constituent search silently degrades to the small built-in offline subset
instead of the full index universe.

Usage::

    uv run python scripts/build_deployment_snapshot.py
    uv run python scripts/build_deployment_snapshot.py --scan-only
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATABASE = REPO_ROOT / "data" / "marketsentinel.db"
LIVE_CACHE = REPO_ROOT / "data" / "constituents_cache.json"
DEPLOY_DIR = REPO_ROOT / "deploy"
SNAPSHOT_DATABASE = DEPLOY_DIR / "public-snapshot.db"
SNAPSHOT_CACHE = DEPLOY_DIR / "constituents_cache.json"

# Credential and personal-data shapes. The schema stores public news metadata and model output
# only, so any hit here means something unexpected reached the corpus and the artifact must not
# ship until it is explained.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{16,}"),
    "github_token": re.compile(
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    ),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "authorization_header": re.compile(r"[Aa]uthorization\s*[:=]\s*['\"]?Bearer\s+\S+"),
    "inline_secret_assignment": re.compile(
        r"\b(?:api[_-]?key|apikey|secret|passwd|password)\s*[:=]\s*['\"][^'\"]{8,}"
    ),
    "credentialed_url": re.compile(
        r"[?&](?:access_token|api_key|apikey|auth|token|key)=[A-Za-z0-9_\-]{12,}"
    ),
    "email_address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    "windows_user_path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+"),
    "posix_home_path": re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+"),
    "private_ip_address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|127\.0\.0\.1)\b"
    ),
}


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def build_snapshot() -> None:
    if not LIVE_DATABASE.is_file():
        raise SystemExit(f"live database not found at {LIVE_DATABASE}")
    if not LIVE_CACHE.is_file():
        raise SystemExit(f"constituent cache not found at {LIVE_CACHE}")

    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    # VACUUM INTO refuses to overwrite, so a rebuild must clear the previous snapshot first.
    SNAPSHOT_DATABASE.unlink(missing_ok=True)
    with _read_only(LIVE_DATABASE) as connection:
        connection.execute("VACUUM INTO ?", (SNAPSHOT_DATABASE.as_posix(),))

    shutil.copy2(LIVE_CACHE, SNAPSHOT_CACHE)

    live_mb = LIVE_DATABASE.stat().st_size / 1_048_576
    snapshot_mb = SNAPSHOT_DATABASE.stat().st_size / 1_048_576
    print(f"database : {LIVE_DATABASE} ({live_mb:.2f} MB)")
    print(f"        -> {SNAPSHOT_DATABASE} ({snapshot_mb:.2f} MB, VACUUM INTO)")
    print(f"cache    : {LIVE_CACHE} -> {SNAPSHOT_CACHE}")


def describe_snapshot() -> None:
    with _read_only(SNAPSHOT_DATABASE) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        print(f"\ntables: {tables}")
        print(f"user_version: {connection.execute('PRAGMA user_version').fetchone()[0]}")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {integrity}")

        print("\nrows per table")
        for table in tables:
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<34} {count:>7}")

        print("\ncoverage per ticker")
        for row in connection.execute(
            "SELECT ticker, COUNT(*) AS n, SUM(is_demo) AS demo,"
            " MIN(published_at) AS lo, MAX(published_at) AS hi"
            " FROM articles GROUP BY ticker ORDER BY n DESC"
        ):
            print(
                f"  {row['ticker']:<8} articles={row['n']:>5}  demo={row['demo']:>3}"
                f"  {row['lo'][:10]} .. {row['hi'][:10]}"
            )


def scan_artifacts() -> bool:
    """Report any credential or personal-data shape found in either artifact."""

    findings: dict[str, list[str]] = {name: [] for name in SECRET_PATTERNS}
    scanned_values = 0

    def inspect(origin: str, value: str) -> None:
        nonlocal scanned_values
        scanned_values += 1
        for name, pattern in SECRET_PATTERNS.items():
            for match in pattern.findall(value):
                if len(findings[name]) < 5:
                    findings[name].append(f"{origin}: {match[:100]}")

    with _read_only(SNAPSHOT_DATABASE) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for row in connection.execute(f"SELECT * FROM {table}"):
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, str):
                        inspect(f"{table}.{column}", value)

    cache_text = SNAPSHOT_CACHE.read_text(encoding="utf-8")
    inspect("constituents_cache.json", cache_text)
    cache_payload = json.loads(cache_text)
    constituents = cache_payload.get("constituents", [])
    markets = Counter(item.get("market") for item in constituents)
    print(f"\nconstituent cache: {len(constituents)} constituents {dict(markets)}")
    print(
        f"  source={cache_payload.get('source')!r} is_fallback={cache_payload.get('is_fallback')}"
    )

    print(f"\nsecret and personal-data scan over {scanned_values:,} values")
    clean = True
    for name in SECRET_PATTERNS:
        hits = findings[name]
        if hits:
            clean = False
            print(f"  FAIL {name}: {len(hits)}+ match(es)")
            for hit in hits:
                print(f"         {hit}")
        else:
            print(f"  ok   {name}")

    print(f"\nRESULT: {'CLEAN' if clean else 'REVIEW REQUIRED -- do not commit'}")
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="scan the existing deploy/ artifacts without rebuilding them",
    )
    arguments = parser.parse_args()

    if not arguments.scan_only:
        build_snapshot()
    elif not SNAPSHOT_DATABASE.is_file():
        raise SystemExit(f"no snapshot to scan at {SNAPSHOT_DATABASE}")

    describe_snapshot()
    return 0 if scan_artifacts() else 1


if __name__ == "__main__":
    sys.exit(main())
