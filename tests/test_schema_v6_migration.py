"""The v5 -> v6 schema migration (MR-006): one additive table, nothing else touched.

A frozen copy of the version-5 DDL is built on a throwaway database, filled with a row in every
v5 table, and opened by the current build. Fully offline; no live database is read.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from conftest import make_article

from marketsentinel.domain import CompanyReference, CompanyRole, CompanyRoleLabel
from marketsentinel.storage.sqlite import SCHEMA_USER_VERSION, SQLiteRepository

# Verbatim copy of the version-5 schema, frozen here so the test does not drift with the code.
V5_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS articles (
    fingerprint TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    source TEXT NOT NULL,
    provider_article_id TEXT,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    relevance_score REAL NOT NULL CHECK (relevance_score BETWEEN 0 AND 1),
    snippet TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_ticker_published
    ON articles (ticker, published_at DESC);

CREATE TABLE IF NOT EXISTS sentiments (
    article_fingerprint TEXT PRIMARY KEY REFERENCES articles(fingerprint) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (label IN ('positive', 'negative', 'neutral')),
    positive REAL NOT NULL,
    negative REAL NOT NULL,
    neutral REAL NOT NULL,
    sentiment_score REAL NOT NULL,
    model_name TEXT NOT NULL,
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_sentiment (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    score REAL NOT NULL,
    moving_average_7d REAL NOT NULL,
    trend_3 REAL NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL,
    positive_share REAL NOT NULL DEFAULT 0,
    negative_share REAL NOT NULL DEFAULT 0,
    weighted_disagreement REAL NOT NULL DEFAULT 0,
    aggregate_weight REAL NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_sentiment_ticker_date
    ON daily_sentiment (ticker, date DESC);

CREATE TABLE IF NOT EXISTS article_intelligence_analyses (
    article_fingerprint TEXT NOT NULL REFERENCES articles(fingerprint) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    cache_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_fingerprint, model_version, cache_version, schema_version)
);

CREATE INDEX IF NOT EXISTS idx_article_intelligence_analyses_lookup
    ON article_intelligence_analyses (article_fingerprint, model_version, cache_version, schema_version);

-- Version 5: the coverage ledger. Operational state only -- no materiality verdict, group, rank,
-- or risk score is ever stored here; those stay recomputed from stored analyses.
CREATE TABLE IF NOT EXISTS company_coverage (
    ticker TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    ledger_started_at TEXT NOT NULL,
    live_window_days INTEGER NOT NULL CHECK (live_window_days > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_watermarks (
    ticker TEXT NOT NULL,
    provider TEXT NOT NULL,
    ingested_through TEXT,
    last_window_start TEXT,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_status TEXT NOT NULL CHECK (last_status IN ('ok', 'partial', 'empty', 'failed')),
    last_message TEXT,
    last_articles INTEGER NOT NULL DEFAULT 0,
    last_request_limited INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, provider)
);

CREATE TABLE IF NOT EXISTS article_analysis_jobs (
    article_fingerprint TEXT NOT NULL REFERENCES articles(fingerprint) ON DELETE CASCADE,
    analysis_contract TEXT NOT NULL,
    ticker TEXT NOT NULL,
    published_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'leased', 'retry_wait', 'analyzed', 'skipped', 'failed', 'baseline')
    ),
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_failure_category TEXT,
    last_failure_at TEXT,
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    analysis_evidence_fingerprint TEXT,
    analysis_created_at TEXT,
    evidence_checked_at TEXT,
    evidence_current INTEGER CHECK (evidence_current IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT,
    PRIMARY KEY (article_fingerprint, analysis_contract)
);

CREATE INDEX IF NOT EXISTS idx_article_analysis_jobs_ticker_state
    ON article_analysis_jobs (ticker, analysis_contract, state);

PRAGMA user_version = 5;
"""

V5_TABLES = (
    "articles",
    "sentiments",
    "daily_sentiment",
    "article_intelligence_analyses",
    "company_coverage",
    "ingestion_watermarks",
    "article_analysis_jobs",
)
NOW = "2026-09-01T12:00:00+00:00"
CONTRACT = "stage-abc-contract"
KEY = ("model-x", "prompt-x", "schema-x")


def build_v5_database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(V5_SCHEMA)
        connection.execute(
            "INSERT INTO articles (fingerprint, ticker, title, normalized_title, url,"
            " normalized_url, source, provider_article_id, published_at, fetched_at, provider,"
            " relevance_score, snippet, is_demo)"
            " VALUES ('fp1', 'ACME', 'Acme wins', 'acme wins', 'https://x.example/1',"
            " 'x.example/1', 'Wire', 'p1', ?, ?, 'test', 0.9, 'snippet', 0)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO sentiments VALUES ('fp1', 'positive', 0.8, 0.1, 0.1, 0.7, 'm', ?)", (NOW,)
        )
        connection.execute(
            "INSERT INTO daily_sentiment VALUES ('ACME', '2026-09-01', 0.5, 0.4, 0.1, 3, 0.6, 0.1,"
            " 0.2, 1.5, ?)",
            (NOW,),
        )
        connection.execute(
            "INSERT INTO article_intelligence_analyses VALUES ('fp1', 'm', 'c1', 's1', '{}', ?)",
            (NOW,),
        )
        connection.execute("INSERT INTO company_coverage VALUES ('ACME', 1, ?, 30, ?)", (NOW, NOW))
        connection.execute(
            "INSERT INTO ingestion_watermarks VALUES ('ACME', 'test', ?, ?, ?, ?, 'ok', 'fine', 4,"
            " 0, 0)",
            (NOW, NOW, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO article_analysis_jobs (article_fingerprint, analysis_contract, ticker,"
            " published_at, state, attempts, input_tokens, output_tokens, created_at, updated_at)"
            " VALUES ('fp1', ?, 'ACME', ?, 'analyzed', 1, 1200, 300, ?, ?)",
            (CONTRACT, NOW, NOW, NOW),
        )


def dump(path, tables=V5_TABLES):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


def schema_objects(path):
    with closing(sqlite3.connect(path)) as connection:
        return dict(
            connection.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )


def user_version(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def make_label(fingerprint, **updates):
    label = CompanyRoleLabel(
        article_id=fingerprint,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        role=CompanyRole.PRINCIPAL,
        confidence=0.9,
        rationale="Acme is the acquirer.",
        model_version=KEY[0],
        prompt_version=KEY[1],
        schema_version=KEY[2],
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    return label.model_copy(update=updates) if updates else label


def fresh(tmp_path):
    repository = SQLiteRepository(tmp_path / "roles.db")
    repository.initialize()
    item = make_article(title="Acme Corporation buys a supplier")
    repository.upsert_articles([item])
    return repository, item


# --- the migration -----------------------------------------------------------------------------


def test_the_fixture_really_is_a_version_5_database_without_the_role_table(writable_tmp_path):
    path = writable_tmp_path / "v5.db"
    build_v5_database(path)

    assert user_version(path) == 5
    assert "article_company_roles" not in schema_objects(path)
    assert all(rows for rows in dump(path).values())  # a row in every v5 table


def test_the_build_is_at_version_6():
    assert SCHEMA_USER_VERSION == 6


def test_initialize_migrates_v5_to_v6_additively_and_idempotently(writable_tmp_path):
    path = writable_tmp_path / "v5.db"
    build_v5_database(path)
    rows_before = dump(path)
    objects_before = schema_objects(path)
    repository = SQLiteRepository(path)
    summary_before = repository.analysis_job_summary("ACME", CONTRACT)

    repository.initialize()
    after_first = (dump(path), schema_objects(path))
    repository.initialize()  # a second run is a no-op

    assert (dump(path), schema_objects(path)) == after_first
    # Every v5 row is unchanged, byte for byte, in every v5 table.
    assert dump(path) == rows_before
    # The only schema difference is the new table and its index; no v5 object was altered.
    objects_after = schema_objects(path)
    assert set(objects_after) - set(objects_before) == {
        "article_company_roles",
        "idx_article_company_roles_subject",
    }
    assert {n: sql for n, sql in objects_after.items() if n in objects_before} == objects_before
    # The new table exists and is empty; the version is bumped.
    assert dump(path, ("article_company_roles",)) == {"article_company_roles": []}
    assert user_version(path) == 6
    # The Stage A/B/C ledger is unchanged.
    assert repository.analysis_job_summary("ACME", CONTRACT) == summary_before
    assert summary_before.input_tokens == 1200


def test_a_migrated_database_stores_and_reads_labels(writable_tmp_path):
    path = writable_tmp_path / "v5.db"
    build_v5_database(path)
    repository = SQLiteRepository(path)
    repository.initialize()

    label = make_label("fp1")

    assert repository.store_company_role(label) is True
    assert repository.get_company_role("fp1", *KEY) == label


# --- the store ---------------------------------------------------------------------------------


def test_a_label_round_trips_and_an_unlabelled_article_is_absent(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)
    label = make_label(item.fingerprint)

    assert repository.get_company_role(item.fingerprint, *KEY) is None
    assert repository.store_company_role(label) is True

    assert repository.get_company_role(item.fingerprint, *KEY) == label
    assert repository.list_company_roles("ACME", *KEY) == {item.fingerprint: label}
    # Another contract or ticker sees nothing: unlabelled, never "mentioned".
    assert repository.list_company_roles("ACME", "model-x", "prompt-y", "schema-x") == {}
    assert repository.list_company_roles("OTHER", *KEY) == {}


def test_a_stored_label_is_immutable(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)
    first = make_label(item.fingerprint)
    repository.store_company_role(first)

    changed = make_label(item.fingerprint, role=CompanyRole.MENTIONED, rationale="changed")

    assert repository.store_company_role(changed) is False
    assert repository.get_company_role(item.fingerprint, *KEY) == first


def test_a_new_contract_adds_rows_beside_the_old_ones(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)
    repository.store_company_role(make_label(item.fingerprint))

    newer = make_label(item.fingerprint, prompt_version="prompt-y", role=CompanyRole.MENTIONED)
    assert repository.store_company_role(newer) is True

    old = repository.get_company_role(item.fingerprint, *KEY)
    new = repository.get_company_role(item.fingerprint, "model-x", "prompt-y", "schema-x")
    assert (old.role, new.role) == (CompanyRole.PRINCIPAL, CompanyRole.MENTIONED)


def test_a_row_with_an_unknown_role_reads_as_unlabelled(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)
    repository.store_company_role(make_label(item.fingerprint))
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute("UPDATE article_company_roles SET role = 'counterparty'")

    assert repository.get_company_role(item.fingerprint, *KEY) is None
    assert repository.list_company_roles("ACME", *KEY) == {}


def test_confidence_is_bounded_by_the_table(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)

    with (
        closing(repository._connect()) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "INSERT INTO article_company_roles VALUES (?, 'm', 'p', 's', 'ACME', 'Acme',"
            " 'principal', 1.5, 'r', ?)",
            (item.fingerprint, NOW),
        )


def test_labels_are_removed_with_their_article(writable_tmp_path):
    repository, item = fresh(writable_tmp_path)
    repository.store_company_role(make_label(item.fingerprint))

    with closing(repository._connect()) as connection, connection:
        connection.execute("DELETE FROM articles WHERE fingerprint = ?", (item.fingerprint,))

    assert repository.get_company_role(item.fingerprint, *KEY) is None
