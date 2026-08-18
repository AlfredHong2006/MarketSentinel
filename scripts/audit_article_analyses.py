"""Summarize stored article analyses for deterministic offline QA."""

import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from marketsentinel.event_policy import is_meaningful_event_values


def _print_counts(label: str, values: Iterable[str]) -> None:
    counts = Counter(values)
    print(f"\n{label}")
    for value, count in sorted(counts.items()):
        print(f"  {value}: {count}")


def _quality_metrics(version: str, records: list[dict[str, Any]]) -> None:
    """Report generation-specific quality for one Stage A prompt version only."""

    meaningful = sum(
        is_meaningful_event_values(
            item.get("event", {}).get("event_type", "uncertain"),
            item.get("event", {}).get("magnitude", 0.0),
            item.get("event", {}).get("model_confidence", 0.0),
        )
        for item in records
    )
    print(f"\nquality for stage_a_prompt_version={version} (analyses: {len(records)})")
    print(f"  meaningful events: {meaningful}/{len(records)} ({meaningful / len(records):.1%})")
    for field in ("direction", "event_type", "time_horizon"):
        _print_counts(
            f"  {field}",
            (str(item.get("event", {}).get(field, "missing")) for item in records),
        )
    _print_counts(
        "  channel presence",
        (
            presence
            for item in records
            for presence in (
                "positive_present"
                if item.get("event", {}).get("positive_channels")
                else "positive_absent",
                "negative_present"
                if item.get("event", {}).get("negative_channels")
                else "negative_absent",
            )
        ),
    )


def _load_records(database: Path, ticker: str | None) -> list[dict[str, Any]]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        query = """
            SELECT analyses.analysis_json
            FROM article_intelligence_analyses AS analyses
            JOIN articles AS articles
              ON articles.fingerprint = analyses.article_fingerprint
        """
        parameters: tuple[str, ...] = ()
        if ticker:
            query += " WHERE articles.ticker = ?"
            parameters = (ticker.upper(),)
        query += " ORDER BY analyses.created_at, analyses.rowid"
        rows = connection.execute(query, parameters).fetchall()
    return [json.loads(str(row[0])) for row in rows]


def summarize(database: Path, ticker: str | None = None, stage_a: str | None = None) -> None:
    records = _load_records(database, ticker)
    if stage_a:
        records = [item for item in records if item.get("stage_a_prompt_version") == stage_a]
    print(f"database: {database}")
    print(f"ticker: {ticker.upper() if ticker else 'ALL'}")
    print(f"stage_a_prompt_version filter: {stage_a or 'NONE (metrics segmented per version)'}")
    print(f"analyses: {len(records)}")
    if not records:
        return

    for field in (
        "stage_a_prompt_version",
        "stage_b_prompt_version",
        "stage_c_prompt_version",
        "schema_version",
        "source_class",
    ):
        _print_counts(field, (str(item.get(field, "missing")) for item in records))

    # Quality metrics describe one model generation at a time. Pooling them across Stage A prompt
    # versions would misrepresent a mixed corpus as a single generation's behaviour.
    versions = sorted({str(item.get("stage_a_prompt_version", "missing")) for item in records})
    for version in versions:
        _quality_metrics(
            version,
            [
                item
                for item in records
                if str(item.get("stage_a_prompt_version", "missing")) == version
            ],
        )

    _print_counts(
        "claim statuses",
        (
            str(claim.get("status", "missing"))
            for item in records
            for claim in item.get("claims", [])
        ),
    )

    strengths = [
        float(item["evidence_strength"]) for item in records if "evidence_strength" in item
    ]
    print("\nevidence strength")
    print(f"  present: {len(strengths)}")
    print(f"  missing: {len(records) - len(strengths)}")
    if strengths:
        print(f"  min: {min(strengths):.2f}")
        print(f"  mean: {sum(strengths) / len(strengths):.2f}")
        print(f"  max: {max(strengths):.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/marketsentinel.db"))
    parser.add_argument("--ticker")
    parser.add_argument(
        "--stage-a",
        dest="stage_a",
        help="Restrict every metric to one Stage A prompt version, e.g. event-extraction-v5.",
    )
    arguments = parser.parse_args()
    summarize(arguments.database, arguments.ticker, arguments.stage_a)


if __name__ == "__main__":
    main()
