"""Build the immutable, content-addressed artifacts the scheduled coverage workflow uploads to R2.

Reuses ``scripts/build_deployment_snapshot.py`` end to end -- the same ``VACUUM INTO`` snapshot,
the same SQLite integrity check, and the same credential/personal-data scan the manually committed
``deploy/`` artifacts go through -- so a CI-published snapshot is held to exactly the same bar, not
a second hand-rolled one. It never touches ``deploy/`` or the live local database; every path is
explicit.

Produces, under ``--output-dir``::

    <version>/public-snapshot.db
    <version>/constituents_cache.json
    latest.json            # manifest: version, sha256, size, and full URLs for both files
    version.txt            # just the version string, for the calling workflow step to read

``version`` is a timestamp plus a content hash of the built snapshot, so it is unique and
immutable by construction -- the workflow uploads the ``<version>/`` files first and overwrites
``latest.json`` last, so a reader never observes a manifest pointing at an object that is not yet
there, and a failed or aborted publish never touches the previously published ``latest.json``.

This script only builds and validates local files; it does not talk to R2 itself, so it needs no
cloud credentials and no new dependency (see the coverage workflow for the upload steps).

Usage:
    uv run python scripts/publish_public_snapshot.py \\
        --database data/marketsentinel.db \\
        --constituent-cache data/constituents_cache.json \\
        --output-dir dist/public-snapshot \\
        --public-base-url https://pub-xxxxxxxx.r2.dev \\
        --tickers NVDA,PFE

Exits non-zero, and writes neither ``latest.json`` nor ``version.txt``, if the built snapshot
fails its integrity check or the secret/personal-data scan -- so the workflow's own success check
on this step is sufficient to gate the upload steps that follow it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# scripts/ has no __init__.py; make the repository root importable so this resolves the same
# "scripts.build_deployment_snapshot" module whether run directly (uv run python scripts/...) or
# imported under pytest (which already puts the root on sys.path via pyproject's pythonpath).
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from marketsentinel.storage.sqlite import SCHEMA_USER_VERSION  # noqa: E402
from scripts.build_deployment_snapshot import (  # noqa: E402
    build_snapshot,
    describe_snapshot,
    scan_artifacts,
)


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def publish(
    *,
    database: Path,
    constituent_cache: Path,
    output_dir: Path,
    public_base_url: str,
    tickers: list[str],
    now: datetime,
) -> int:
    if not database.is_file():
        print(f"database not found: {database}", file=sys.stderr)
        return 2
    if not constituent_cache.is_file():
        print(f"constituent cache not found: {constituent_cache}", file=sys.stderr)
        return 2

    staging_database = output_dir / "_staging" / "public-snapshot.db"
    staging_cache = output_dir / "_staging" / "constituents_cache.json"
    build_snapshot(
        live_database=database,
        live_cache=constituent_cache,
        snapshot_database=staging_database,
        snapshot_cache=staging_cache,
    )
    describe_snapshot(staging_database)
    if not scan_artifacts(staging_database, staging_cache):
        print("\nREFUSING TO PUBLISH: secret or personal-data scan failed", file=sys.stderr)
        return 1

    db_sha256, db_size = _sha256_and_size(staging_database)
    version = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{db_sha256[:12]}"

    version_dir = output_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)
    versioned_database = version_dir / "public-snapshot.db"
    versioned_cache = version_dir / "constituents_cache.json"
    staging_database.replace(versioned_database)
    staging_cache.replace(versioned_cache)
    (output_dir / "_staging").rmdir()

    cache_sha256, cache_size = _sha256_and_size(versioned_cache)
    base_url = public_base_url.rstrip("/")
    manifest = {
        "version": version,
        "published_at": now.isoformat(),
        "schema_user_version": SCHEMA_USER_VERSION,
        "tickers": tickers,
        "database": {
            "url": f"{base_url}/snapshots/{version}/public-snapshot.db",
            "sha256": db_sha256,
            "size_bytes": db_size,
        },
        "constituents_cache": {
            "url": f"{base_url}/snapshots/{version}/constituents_cache.json",
            "sha256": cache_sha256,
            "size_bytes": cache_size,
        },
    }
    (output_dir / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output_dir / "version.txt").write_text(version, encoding="utf-8")

    print(f"\npublished version: {version}")
    print(f"  {versioned_database}")
    print(f"  {versioned_cache}")
    print(f"  manifest -> {output_dir / 'latest.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--database", type=Path, required=True, help="Private operational SQLite database."
    )
    parser.add_argument(
        "--constituent-cache", type=Path, required=True, help="Constituent cache JSON."
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Local staging directory for CI."
    )
    parser.add_argument(
        "--public-base-url",
        required=True,
        help="Public HTTPS base URL the R2 public bucket is served from (no trailing slash needed).",
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="Comma-separated tickers this publish covers, recorded in the manifest only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    tickers = [t.strip().upper() for t in arguments.tickers.split(",") if t.strip()]
    return publish(
        database=arguments.database,
        constituent_cache=arguments.constituent_cache,
        output_dir=arguments.output_dir,
        public_base_url=arguments.public_base_url,
        tickers=tickers,
        now=datetime.now(UTC),
    )


if __name__ == "__main__":
    sys.exit(main())
