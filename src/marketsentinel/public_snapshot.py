"""Runtime fetch of the published public snapshot, with safe fallback to the image's own copy.

The public Docker image ships a frozen ``deploy/public-snapshot.db`` baked in at build time (see
the Dockerfile and ``scripts/build_deployment_snapshot.py``). This module is the other half of the
scheduled-coverage milestone: at container startup, it best-effort replaces that baked-in snapshot
with the latest one published by the coverage workflow to Cloudflare R2, verifying it end to end
first.

Deliberately conservative: this never raises and never blocks startup. Any failure -- the manifest
is unreachable, a hash does not match, the database fails SQLite's own integrity check, or its
schema version does not match this code's -- leaves ``database_path`` / ``constituent_cache_path``
exactly as the image shipped them, so a bad or unreachable publish degrades to "yesterday's data"
rather than a broken deployment.

Manifest shape (published by ``scripts/publish_public_snapshot.py``)::

    {
      "version": "20260913T140000Z-a1b2c3d4",
      "published_at": "2026-09-13T14:00:00+00:00",
      "schema_user_version": 5,
      "tickers": ["NVDA", "PFE"],
      "database": {"url": "...", "sha256": "...", "size_bytes": 123},
      "constituents_cache": {"url": "...", "sha256": "...", "size_bytes": 456}
    }

Asset ``url`` values may be absolute or relative (e.g. root-relative
``/snapshots/<version>/public-snapshot.db``); relative ones resolve against the manifest URL.

Invoked as ``python -m marketsentinel.public_snapshot`` from the Docker image's CMD, before
uvicorn starts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
from pydantic import BaseModel, Field

from marketsentinel.config import Settings, get_settings
from marketsentinel.storage.sqlite import SCHEMA_USER_VERSION, integrity_check

LOGGER = logging.getLogger(__name__)


class SnapshotAsset(BaseModel):
    url: str
    sha256: str
    size_bytes: int = Field(ge=0)


class SnapshotManifest(BaseModel):
    version: str
    published_at: str
    schema_user_version: int
    database: SnapshotAsset
    constituents_cache: SnapshotAsset


@dataclass(frozen=True)
class FetchOutcome:
    """What happened, for the caller to log. Never carries a stack trace to stdout/stderr."""

    applied: bool
    reason: str
    version: str | None = None


def fetch_public_snapshot(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> FetchOutcome:
    """Best-effort refresh of the baked-in public snapshot from the published manifest.

    Never raises. On any failure the files already at ``settings.database_path`` /
    ``settings.constituent_cache_path`` -- the image's baked-in snapshot, or whatever this
    function last wrote -- are left completely untouched.
    """

    if not settings.public_snapshot_manifest_url:
        return FetchOutcome(applied=False, reason="not configured")

    owns_client = client is None
    http = client or httpx.Client(
        timeout=settings.public_snapshot_fetch_timeout_seconds, follow_redirects=True
    )
    try:
        manifest_url = settings.public_snapshot_manifest_url
        manifest = _load_manifest(http, manifest_url)
        if manifest.schema_user_version != SCHEMA_USER_VERSION:
            return FetchOutcome(
                applied=False,
                reason=(
                    f"schema mismatch: manifest has {manifest.schema_user_version}, "
                    f"this build expects {SCHEMA_USER_VERSION}"
                ),
                version=manifest.version,
            )

        db_bytes = _download_verified(http, manifest.database, manifest_url=manifest_url)
        cache_bytes = _download_verified(
            http, manifest.constituents_cache, manifest_url=manifest_url
        )
        cache_text = cache_bytes.decode("utf-8")
        json.loads(cache_text)  # must be well-formed JSON before it replaces anything

        database_path = settings.database_path
        cache_path = settings.constituent_cache_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        with NamedTemporaryFile(dir=database_path.parent, delete=False) as handle:
            handle.write(db_bytes)
            temp_database_path = Path(handle.name)
        try:
            result = integrity_check(temp_database_path)
            if result != "ok":
                raise ValueError(f"downloaded database failed integrity_check: {result}")

            # Clear any WAL/SHM siblings from a previous snapshot at this path: replacing the
            # main file while stale siblings remain could otherwise let SQLite recover the wrong
            # journal on next open.
            for suffix in ("-wal", "-shm"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)

            temp_database_path.replace(database_path)
            cache_path.write_text(cache_text, encoding="utf-8")
        finally:
            temp_database_path.unlink(missing_ok=True)

        return FetchOutcome(applied=True, reason="ok", version=manifest.version)
    except Exception as exc:  # defensive: any failure keeps the image's baked-in snapshot
        return FetchOutcome(applied=False, reason=f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            http.close()


def _load_manifest(http: httpx.Client, url: str) -> SnapshotManifest:
    response = http.get(url)
    response.raise_for_status()
    return SnapshotManifest.model_validate(response.json())


def _download_verified(http: httpx.Client, asset: SnapshotAsset, *, manifest_url: str) -> bytes:
    # RFC 3986 resolution: a relative asset URL is resolved against the manifest URL, while an
    # absolute one is returned unchanged.
    url = httpx.URL(manifest_url).join(asset.url)
    response = http.get(url)
    response.raise_for_status()
    content = response.content
    digest = hashlib.sha256(content).hexdigest()
    if digest != asset.sha256:
        raise ValueError(f"sha256 mismatch for {url}: expected {asset.sha256}, got {digest}")
    return content


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outcome = fetch_public_snapshot(get_settings())
    if outcome.applied:
        LOGGER.info("public snapshot: applied version=%s", outcome.version)
    else:
        LOGGER.info(
            "public snapshot: not applied (%s) -- keeping the image's baked-in snapshot",
            outcome.reason,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
