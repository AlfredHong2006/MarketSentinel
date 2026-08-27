"""Evaluate the deterministic materiality gate against a hand-labelled gold census.

Two subcommands, both read-only. ``worksheet`` prints a labelling skeleton built from the stored
corpus through the product's own loading path, so a human can fill in a verdict per row.
``evaluate`` scores the gate against those filled-in labels.

Three disciplines this tool exists to enforce:

*Drift.* A gold set that silently disagrees with a re-analysed corpus measures nothing. Every
fixture row is compared field by field against the stored analysis it was labelled from, and a
row that has moved is named rather than quietly re-scored.

*Honesty about what is being measured.* Raw metrics are always printed. The accepted-disagreement
adjustment is printed beside them, never instead of them, because the adjustment is an argument
about which errors were knowingly accepted -- not evidence they did not happen.

*Explanation over threshold.* The primary criterion is that no disagreement is unexplained: every
false positive, false negative, and grouping miss must be named in the fixture's
``known_disagreements`` with a reason. Numeric tripwires are secondary, and are checked against
the adjusted figures for exactly the reason the adjustment exists.

The scores here are an in-sample regression pin on one company's labelled corpus, not
out-of-sample validation of the gate.
"""

import argparse
import io
import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.domain import ArticleAnalysis, CompanyIntelligenceEvent
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)
from marketsentinel.event_policy import is_meaningful_event
from marketsentinel.materiality import (
    MaterialityAssessment,
    assess_materiality,
    group_material_events,
)

DEFAULT_DATABASE = Path("data/marketsentinel.db")
DEFAULT_FIXTURE = Path("tests/fixtures/nvda_materiality_gold.json")
DEFAULT_TICKER = "NVDA"

# Both mirror MarketAnalysisService's stored-analysis read. They are restated rather than imported
# because the service reaches them through a read/write repository, and nothing here may write.
DISPLAY_WINDOW_DAYS = 366
STORED_ANALYSES_LIMIT = 500

GATE_PRECISION_FLOOR = 0.85
GATE_RECALL_FLOOR = 0.85
GROUPING_PRECISION_FLOOR = 0.95
GROUPING_RECALL_FLOOR = 0.80

GATE_FALSE_POSITIVE = "gate_false_positive"
GATE_FALSE_NEGATIVE = "gate_false_negative"
GROUPING_FALSE_NEGATIVE = "grouping_false_negative"
GROUPING_FALSE_POSITIVE = "grouping_false_positive"

# Fields a fixture record carries. Everything the gate, the grouping arms, and the ranking key read
# is here; nothing else is. A record is therefore a faithful projection of a stored analysis for
# this layer's purposes, and drift in any of these fields is drift that can change a verdict.
_PLACEHOLDER_SUMMARY = "Not labelled: no materiality rule reads the Stage A summary text."
_PLACEHOLDER_REASONING = "Not labelled: no materiality rule reads Stage B reasoning text."
# Stage A's evidence_strength orders Today's Intelligence, never Key Developments, so the fixture
# does not carry it and reconstruction pins it at zero rather than inventing a value.
_PLACEHOLDER_EVIDENCE_STRENGTH = 0.0
_PLACEHOLDER_CLAIM_CONFIDENCE = 0.0


@dataclass(frozen=True)
class Metrics:
    """One confusion matrix and the two ratios read off it."""

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else 0.0


@dataclass(frozen=True)
class DriftReport:
    """How far the labelled corpus has moved from the corpus stored today."""

    checked: bool
    reason: str = ""
    absent: tuple[str, ...] = ()
    outside_window: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    changed: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.absent or self.outside_window or self.added or self.changed)


@dataclass(frozen=True)
class GroupingReport:
    """Pairwise agreement between gold ``group_id`` and the gate's grouping."""

    scope: int
    gold_pairs: tuple[tuple[str, str], ...]
    predicted_pairs: tuple[tuple[str, str], ...]
    missed: tuple[tuple[str, str], ...]
    spurious: tuple[tuple[str, str], ...]
    excluded_gold_pairs: tuple[tuple[str, str], ...]

    @property
    def raw(self) -> Metrics:
        recovered = len(set(self.gold_pairs) & set(self.predicted_pairs))
        return Metrics(recovered, len(self.spurious), len(self.missed), 0)


@dataclass
class Report:
    """Everything one ``evaluate`` run established, before any of it is printed."""

    ticker: str
    considered: int
    drift: DriftReport
    gate_raw: Metrics
    gate_adjusted: Metrics
    grouping: GroupingReport
    grouping_adjusted: Metrics
    baselines: tuple[tuple[str, Metrics], ...]
    unexplained: tuple[str, ...]
    stale: tuple[str, ...]
    version_mismatch: tuple[str, ...]
    documented: tuple[tuple[str, str], ...]
    failures: list[str] = field(default_factory=list)


def analysis_record(analysis: ArticleAnalysis) -> dict[str, Any]:
    """Project one stored analysis onto the fields this layer reads, and only those."""

    reference = analysis.source_reference
    return {
        "article_id": analysis.article_id,
        "title": reference.title,
        "publisher": reference.publisher,
        "url": reference.url,
        "published_at": reference.published_at.isoformat(),
        "source_class": analysis.source_class.value,
        "subject_company": {
            "symbol": analysis.subject_company.symbol,
            "name": analysis.subject_company.name,
        },
        "event": {
            "event_type": analysis.event.event_type.value,
            "direction": analysis.event.direction.value,
            "magnitude": analysis.event.magnitude,
            "model_confidence": analysis.event.model_confidence,
            "time_horizon": analysis.event.time_horizon.value,
        },
        "claims": [
            {
                "claim_id": claim.claim_id,
                "status": claim.status.value,
                "evidence_article_ids": list(claim.evidence_article_ids),
            }
            for claim in analysis.claims
        ],
        "evidence_sources": [
            {
                "article_id": source.article_id,
                "title": source.title,
                "publisher": source.publisher,
                "published_at": source.published_at.isoformat(),
                "url": source.url,
            }
            for source in analysis.evidence_sources
        ],
    }


def event_from_record(record: Mapping[str, Any]) -> CompanyIntelligenceEvent:
    """Rebuild the typed event a fixture record stands for.

    Fields no materiality rule reads are filled with named placeholders rather than invented
    plausible values, so a reader can never mistake fixture filler for stored data. Validation
    goes through JSON for the same reason ``compatible_intelligence_events`` does: the typed
    contract is strict, so enums and timestamps are only accepted in their serialised form.
    """

    payload = {
        "article_id": record["article_id"],
        "source_reference": {
            "article_id": record["article_id"],
            "title": record["title"],
            "publisher": record["publisher"],
            "published_at": record["published_at"],
            "url": record["url"],
        },
        "source_class": record["source_class"],
        "subject_company": dict(record["subject_company"]),
        "event": {**dict(record["event"]), "summary": _PLACEHOLDER_SUMMARY},
        "claims": [
            {
                "claim_id": claim["claim_id"],
                "status": claim["status"],
                "evidence_article_ids": list(claim["evidence_article_ids"]),
                "reasoning": _PLACEHOLDER_REASONING,
                "confidence": _PLACEHOLDER_CLAIM_CONFIDENCE,
            }
            for claim in record["claims"]
        ],
        "related_companies": [],
        "evidence_strength": _PLACEHOLDER_EVIDENCE_STRENGTH,
        "evidence_sources": [dict(source) for source in record["evidence_sources"]],
    }
    return CompanyIntelligenceEvent.model_validate_json(json.dumps(payload))


def stored_records(
    database: Path,
    ticker: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read the corpus the product would display, without opening the database for writing.

    The query, ordering, per-article deduplication, compatibility filter, and limit mirror
    ``SqliteRepository.list_article_analyses`` as the service calls it. Opening the repository
    itself would run its schema statements, which is a write.
    """

    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=DISPLAY_WINDOW_DAYS)
    compatibility = ArticleAnalysisCompatibility(
        model_version="",  # never consulted by accepts_for_display
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT analyses.article_fingerprint, analyses.analysis_json
            FROM article_intelligence_analyses AS analyses
            JOIN articles AS a
              ON a.fingerprint = analyses.article_fingerprint
            WHERE a.ticker = ? AND a.is_demo = 0 AND a.published_at >= ?
            ORDER BY a.published_at DESC, analyses.created_at DESC, analyses.rowid DESC
            """,
            (ticker.upper(), since.isoformat()),
        ).fetchall()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        article_id = str(row["article_fingerprint"])
        if article_id in seen:
            continue
        try:
            analysis = ArticleAnalysis.model_validate_json(row["analysis_json"])
        except ValidationError:
            continue
        if not compatibility.accepts_for_display(analysis):
            continue
        seen.add(article_id)
        records.append(analysis_record(analysis))
        if len(records) >= STORED_ANALYSES_LIMIT:
            break
    return records


def load_gold(fixture: Path) -> dict[str, Any]:
    """Read a gold set, refusing one that cannot be scored honestly."""

    gold = json.loads(fixture.read_text(encoding="utf-8"))
    records = gold["records"]
    unlabelled = [item for item in records if item.get("label", {}).get("material") is None]
    if unlabelled:
        raise ValueError(f"{len(unlabelled)} fixture rows are still unlabelled")
    # Every structure below is keyed by article id, so a duplicate would not raise -- it would
    # quietly drop a row from the corpus and report a smaller one as complete.
    identifiers = [item["article_id"] for item in records]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("fixture rows share an article_id")
    return gold


def detect_drift(
    records: Sequence[Mapping[str, Any]],
    stored: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> DriftReport:
    """Compare each labelled row against the stored analysis it was labelled from.

    A row that has left the product's rolling display window is reported apart from one that is
    genuinely absent: the first is the window moving, the second is the corpus changing.
    """

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=DISPLAY_WINDOW_DAYS)
    by_id = {item["article_id"]: item for item in stored}
    absent: list[str] = []
    outside: list[str] = []
    changed: list[tuple[str, tuple[str, ...]]] = []
    for record in records:
        current = by_id.get(record["article_id"])
        if current is None:
            published = datetime.fromisoformat(record["published_at"])
            (outside if published < cutoff else absent).append(record["article_id"])
            continue
        fields = tuple(_changed_fields(record, current))
        if fields:
            changed.append((record["article_id"], fields))
    known = {item["article_id"] for item in records}
    added = [item["article_id"] for item in stored if item["article_id"] not in known]
    return DriftReport(
        checked=True,
        absent=tuple(absent),
        outside_window=tuple(outside),
        added=tuple(added),
        changed=tuple(changed),
    )


def gate_metrics(
    verdicts: Mapping[str, MaterialityAssessment],
    gold: Mapping[str, bool],
    *,
    excluded: Iterable[str] = (),
) -> Metrics:
    skip = set(excluded)
    return _metrics_from_predictions(
        {article_id: verdicts[article_id].material for article_id in gold},
        {article_id: expected for article_id, expected in gold.items() if article_id not in skip},
    )


def grouping_report(
    events: Sequence[CompanyIntelligenceEvent],
    verdicts: Mapping[str, MaterialityAssessment],
    gold_material: Mapping[str, bool],
    gold_groups: Mapping[str, str],
) -> GroupingReport:
    """Score grouping only where gold and gate agree a row is a development at all.

    Outside that intersection a gold group_id has no predicted counterpart, so scoring it would
    charge one gate error twice -- once against the gate, once against the grouping. Gold pairs
    that the restriction drops are reported rather than dropped silently.
    """

    scope = [
        item.article_id
        for item in events
        if gold_material[item.article_id] and verdicts[item.article_id].material
    ]
    in_scope = set(scope)
    predicted_index: dict[str, int] = {}
    for index, group in enumerate(group_material_events(events)):
        for member in group.members:
            predicted_index[member.article_id] = index
    gold_pairs = {
        pair
        for pair in combinations(sorted(in_scope), 2)
        if gold_groups[pair[0]] == gold_groups[pair[1]]
    }
    predicted_pairs = {
        pair
        for pair in combinations(sorted(in_scope), 2)
        if predicted_index[pair[0]] == predicted_index[pair[1]]
    }
    excluded = {
        pair
        for pair in combinations(sorted(gold_groups), 2)
        if gold_groups[pair[0]] == gold_groups[pair[1]] and not in_scope.issuperset(pair)
    }
    return GroupingReport(
        scope=len(scope),
        gold_pairs=tuple(sorted(gold_pairs)),
        predicted_pairs=tuple(sorted(predicted_pairs)),
        missed=tuple(sorted(gold_pairs - predicted_pairs)),
        spurious=tuple(sorted(predicted_pairs - gold_pairs)),
        excluded_gold_pairs=tuple(sorted(excluded)),
    )


def adjusted_grouping_metrics(
    report: GroupingReport,
    accepted: Iterable[frozenset[str]],
) -> Metrics:
    """Grouping metrics with the pairs named in ``known_disagreements`` removed from both sides."""

    documented = set(accepted)
    gold = {pair for pair in report.gold_pairs if frozenset(pair) not in documented}
    predicted = {pair for pair in report.predicted_pairs if frozenset(pair) not in documented}
    return Metrics(len(gold & predicted), len(predicted - gold), len(gold - predicted), 0)


def baseline_meaningful(
    events: Sequence[CompanyIntelligenceEvent],
    gold: Mapping[str, bool],
) -> Metrics:
    """The shared meaningful-event floor alone -- what Today's Intelligence already selects on."""

    predictions = {item.article_id: is_meaningful_event(item.event) for item in events}
    return _metrics_from_predictions(predictions, gold)


def baseline_top_by_magnitude(
    events: Sequence[CompanyIntelligenceEvent],
    gold: Mapping[str, bool],
    *,
    limit: int,
) -> Metrics:
    """The de-facto ranking the product had before this layer: magnitude, then confidence."""

    ordered = sorted(
        events,
        key=lambda item: (
            -item.event.magnitude,
            -item.event.model_confidence,
            item.article_id,
        ),
    )
    chosen = {item.article_id for item in ordered[:limit]}
    return _metrics_from_predictions(
        {item.article_id: item.article_id in chosen for item in events}, gold
    )


def evaluate(
    fixture: Path,
    *,
    database: Path | None = None,
    now: datetime | None = None,
) -> Report:
    """Score the gate on one labelled corpus and collect every reason it could be wrong."""

    gold = load_gold(fixture)
    records = list(gold["records"])
    events = [event_from_record(record) for record in records]
    verdicts = {item.article_id: assess_materiality(item) for item in events}
    gold_material = {record["article_id"]: bool(record["label"]["material"]) for record in records}
    # Namespaced so a group slug can never collide with the article id a singleton falls back to.
    gold_groups = {
        record["article_id"]: (
            f"group:{record['label']['group_id']}"
            if record["label"]["group_id"]
            else f"row:{record['article_id']}"
        )
        for record in records
    }
    disagreements = list(gold.get("known_disagreements", []))
    gate_documented = {
        item["article_id"]: item
        for item in disagreements
        if item["kind"] in {GATE_FALSE_POSITIVE, GATE_FALSE_NEGATIVE}
    }
    grouping_documented = {
        frozenset(item["article_ids"]): item
        for item in disagreements
        if item["kind"] in {GROUPING_FALSE_NEGATIVE, GROUPING_FALSE_POSITIVE}
    }

    grouping = grouping_report(events, verdicts, gold_material, gold_groups)
    report = Report(
        ticker=str(gold["corpus"]["ticker"]),
        considered=len(records),
        drift=(
            detect_drift(
                records, stored_records(database, str(gold["corpus"]["ticker"]), now=now), now=now
            )
            if database is not None
            else DriftReport(checked=False, reason="no database supplied")
        ),
        gate_raw=gate_metrics(verdicts, gold_material),
        gate_adjusted=gate_metrics(verdicts, gold_material, excluded=gate_documented.keys()),
        grouping=grouping,
        grouping_adjusted=adjusted_grouping_metrics(grouping, grouping_documented),
        baselines=(
            ("meaningful-event floor", baseline_meaningful(events, gold_material)),
            (
                "top-N by magnitude then confidence",
                baseline_top_by_magnitude(
                    events,
                    gold_material,
                    limit=sum(1 for item in verdicts.values() if item.material),
                ),
            ),
        ),
        unexplained=_unexplained(
            verdicts, gold_material, gate_documented, grouping, grouping_documented
        ),
        stale=_stale(verdicts, gold_material, gate_documented, grouping, grouping_documented),
        version_mismatch=_version_mismatch(gold["corpus"]),
        documented=tuple(
            (str(item["kind"]), str(item.get("reason", ""))) for item in disagreements
        ),
    )
    report.failures = _failures(report)
    return report


def render_report(report: Report) -> None:
    print(f"ticker: {report.ticker}")
    print(f"labelled analyses: {report.considered}")
    _render_drift(report.drift)

    print("\ngate confusion (raw)")
    _render_metrics(report.gate_raw)
    print("\ngate confusion (documented disagreements excluded)")
    _render_metrics(report.gate_adjusted)

    print("\nbaselines on the same labels (raw)")
    for name, metrics in report.baselines:
        print(
            f"  {name}: precision {metrics.precision:.3f} | recall {metrics.recall:.3f}"
            f" (tp {metrics.true_positives} fp {metrics.false_positives}"
            f" fn {metrics.false_negatives})"
        )

    grouping = report.grouping
    print(f"\ngrouping pairwise over {grouping.scope} rows gold and gate both call material")
    print(
        f"  gold pairs: {len(grouping.gold_pairs)}"
        f" | predicted pairs: {len(grouping.predicted_pairs)}"
    )
    raw = grouping.raw
    print(f"  raw:      precision {raw.precision:.3f} | recall {raw.recall:.3f}")
    adjusted = report.grouping_adjusted
    print(f"  adjusted: precision {adjusted.precision:.3f} | recall {adjusted.recall:.3f}")
    if raw.recall < GROUPING_RECALL_FLOOR:
        print(
            f"  note: raw recall {raw.recall:.3f} is below the {GROUPING_RECALL_FLOOR:.2f} tripwire."
            " It is accepted only because every missed pair is documented below."
        )
    for pair in grouping.missed:
        print(f"  missed pair: {pair[0][:12]} / {pair[1][:12]}")
    for pair in grouping.spurious:
        print(f"  over-grouped pair: {pair[0][:12]} / {pair[1][:12]}")
    if grouping.excluded_gold_pairs:
        print(f"  gold pairs outside the scored intersection: {len(grouping.excluded_gold_pairs)}")

    print(f"\ndocumented disagreements: {len(report.documented)}")
    for kind, reason in report.documented:
        print(f"  {kind}: {reason}")

    if report.unexplained:
        print(f"\nUNEXPLAINED disagreements: {len(report.unexplained)}")
        for line in report.unexplained:
            print(f"  {line}")
    else:
        print("\nunexplained disagreements: 0")

    print("\nthresholds")
    for line in _threshold_lines(report):
        print(f"  {line}")
    print("\nThese are in-sample regression figures on one labelled corpus, not validation.")
    print("PASS" if not report.failures else "FAIL")
    for failure in report.failures:
        print(f"  {failure}")


def worksheet(database: Path, ticker: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the labelling skeleton, with computed columns marked advisory rather than answers."""

    records = stored_records(database, ticker, now=now)
    events = [event_from_record(record) for record in records]
    verdicts = {item.article_id: assess_materiality(item) for item in events}
    return {
        "description": (
            "Labelling skeleton for the materiality gold set. Fill in label.material, "
            "label.reason, and label.group_id for every row. The advisory block is what the gate "
            "currently says: it is context for the labeller, never the answer, and copying it "
            "would make the evaluation measure nothing."
        ),
        "corpus": {
            "ticker": ticker.upper(),
            "compatible_analyses": len(records),
            "stage_a_prompt_version": STAGE_A_PROMPT_VERSION,
            "stage_b_prompt_version": STAGE_B_PROMPT_VERSION,
            "stage_c_prompt_version": STAGE_C_PROMPT_VERSION,
            "schema_version": ARTICLE_ANALYSIS_SCHEMA_VERSION,
        },
        "known_disagreements": [],
        "records": [
            {
                **record,
                "label": {"material": None, "reason": "", "group_id": None},
                "advisory": _advisory(verdicts[record["article_id"]]),
            }
            for record in records
        ],
    }


def _advisory(assessment: MaterialityAssessment) -> dict[str, Any]:
    """What the gate says about one row, for context.

    Deliberately silent about grouping: group_id is the one column where copying the gate's
    answer would leave the pairwise metric measuring nothing at all.
    """

    return {
        "gate_material": assessment.material,
        "gate_failed_condition": assessment.failed_condition,
        "gate_tier": assessment.tier,
        "gate_rescued_disclosure": assessment.rescued_disclosure,
        "external_sources": assessment.corroboration.external_sources,
        "contradicted_claims": assessment.corroboration.contradicted_claims,
    }


def _metrics_from_predictions(
    predictions: Mapping[str, bool],
    gold: Mapping[str, bool],
) -> Metrics:
    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for article_id, expected in gold.items():
        predicted = predictions[article_id]
        if predicted and expected:
            counts["tp"] += 1
        elif predicted:
            counts["fp"] += 1
        elif expected:
            counts["fn"] += 1
        else:
            counts["tn"] += 1
    return Metrics(counts["tp"], counts["fp"], counts["fn"], counts["tn"])


def _changed_fields(record: Mapping[str, Any], current: Mapping[str, Any]) -> Iterable[str]:
    # The union, not the fixture's own keys: a field added to the projection after a row was
    # labelled is exactly the drift this check exists to catch, and it is absent on that side.
    for key in sorted((set(record) | set(current)) - {"label", "advisory"}):
        if _canonical(record.get(key)) != _canonical(current.get(key)):
            yield key


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _unexplained(
    verdicts: Mapping[str, MaterialityAssessment],
    gold: Mapping[str, bool],
    gate_documented: Mapping[str, Any],
    grouping: GroupingReport,
    grouping_documented: Mapping[frozenset[str], Any],
) -> tuple[str, ...]:
    """Name every disagreement the fixture does not already account for, and its kind."""

    lines: list[str] = []
    for article_id, expected in gold.items():
        predicted = verdicts[article_id].material
        if predicted == expected:
            continue
        kind = GATE_FALSE_POSITIVE if predicted else GATE_FALSE_NEGATIVE
        documented = gate_documented.get(article_id)
        if documented is None:
            lines.append(f"{kind}: {article_id[:12]} is not in known_disagreements")
        elif documented["kind"] != kind:
            lines.append(f"{kind}: {article_id[:12]} is documented as {documented['kind']} instead")
    for pair in grouping.missed:
        if frozenset(pair) not in grouping_documented:
            lines.append(
                f"{GROUPING_FALSE_NEGATIVE}: {pair[0][:12]} / {pair[1][:12]}"
                " is not in known_disagreements"
            )
    for pair in grouping.spurious:
        if frozenset(pair) not in grouping_documented:
            lines.append(
                f"{GROUPING_FALSE_POSITIVE}: {pair[0][:12]} / {pair[1][:12]}"
                " is not in known_disagreements"
            )
    return tuple(lines)


def _version_mismatch(corpus: Mapping[str, Any]) -> tuple[str, ...]:
    """Whether the labels were made against the interpretation the code still runs.

    A prompt or schema version bump re-analyses the corpus, and a label attached to a verdict a
    superseded generation produced describes an article the product no longer stores. This is
    drift the fixture can detect on its own, with no database present.
    """

    running = {
        "stage_a_prompt_version": STAGE_A_PROMPT_VERSION,
        "stage_b_prompt_version": STAGE_B_PROMPT_VERSION,
        "stage_c_prompt_version": STAGE_C_PROMPT_VERSION,
        "schema_version": ARTICLE_ANALYSIS_SCHEMA_VERSION,
    }
    return tuple(
        f"{name}: labelled against {corpus[name]}, running {value}"
        for name, value in running.items()
        if name in corpus and corpus[name] != value
    )


def _stale(
    verdicts: Mapping[str, MaterialityAssessment],
    gold: Mapping[str, bool],
    gate_documented: Mapping[str, Any],
    grouping: GroupingReport,
    grouping_documented: Mapping[frozenset[str], Any],
) -> tuple[str, ...]:
    """Name accepted disagreements that no longer happen.

    A stale entry is not harmless bookkeeping: the adjusted figures are computed by removing
    exactly these rows and pairs, so an entry that has been fixed quietly shrinks the set the
    gate is scored on while still reading as an admission of error.
    """

    lines: list[str] = []
    for article_id, entry in gate_documented.items():
        if article_id not in gold:
            lines.append(f"{entry['kind']}: {article_id[:12]} is not in the labelled corpus")
        elif verdicts[article_id].material == gold[article_id]:
            lines.append(f"{entry['kind']}: {article_id[:12]} now agrees with the label")
    missed = {frozenset(pair) for pair in grouping.missed}
    spurious = {frozenset(pair) for pair in grouping.spurious}
    for pair, entry in grouping_documented.items():
        expected = missed if entry["kind"] == GROUPING_FALSE_NEGATIVE else spurious
        if pair not in expected:
            names = " / ".join(sorted(item[:12] for item in pair))
            lines.append(f"{entry['kind']}: {names} no longer disagrees")
    return tuple(lines)


def _threshold_lines(report: Report) -> list[str]:
    adjusted = report.gate_adjusted
    grouping = report.grouping_adjusted
    checks = [
        ("gate precision", adjusted.precision, GATE_PRECISION_FLOOR),
        ("gate recall", adjusted.recall, GATE_RECALL_FLOOR),
        ("grouping precision", grouping.precision, GROUPING_PRECISION_FLOOR),
        ("grouping recall", grouping.recall, GROUPING_RECALL_FLOOR),
    ]
    lines = [
        f"{name}: {value:.3f} vs floor {floor:.2f} - {'ok' if value >= floor else 'BELOW'}"
        for name, value, floor in checks
    ]
    for name, metrics in report.baselines:
        beaten = report.gate_raw.precision > metrics.precision
        lines.append(
            f"raw gate precision {report.gate_raw.precision:.3f} vs {name}"
            f" {metrics.precision:.3f} - {'ok' if beaten else 'NOT BEATEN'}"
        )
    return lines


def _failures(report: Report) -> list[str]:
    failures: list[str] = []
    if report.unexplained:
        failures.append(f"{len(report.unexplained)} unexplained disagreement(s)")
    if report.stale:
        failures.append(f"{len(report.stale)} accepted disagreement(s) that no longer happen")
    if report.version_mismatch:
        failures.append("the labels were made against a superseded analysis version")
    if report.drift.checked and not report.drift.clean:
        failures.append("the labelled corpus no longer matches the stored corpus")
    if report.gate_adjusted.precision < GATE_PRECISION_FLOOR:
        failures.append("adjusted gate precision below floor")
    if report.gate_adjusted.recall < GATE_RECALL_FLOOR:
        failures.append("adjusted gate recall below floor")
    if report.grouping_adjusted.precision < GROUPING_PRECISION_FLOOR:
        failures.append("adjusted grouping precision below floor")
    if report.grouping_adjusted.recall < GROUPING_RECALL_FLOOR:
        failures.append("adjusted grouping recall below floor")
    for name, metrics in report.baselines:
        if report.gate_raw.precision <= metrics.precision:
            failures.append(f"gate precision does not beat the {name} baseline")
    return failures


def _render_drift(drift: DriftReport) -> None:
    if not drift.checked:
        print(f"drift: not checked ({drift.reason})")
        return
    if drift.clean:
        print("drift: none - every labelled row still matches the stored analysis")
        return
    print("drift: DETECTED")
    for article_id in drift.absent:
        print(f"  absent from the database: {article_id[:12]}")
    for article_id in drift.outside_window:
        print(f"  left the {DISPLAY_WINDOW_DAYS}-day display window: {article_id[:12]}")
    for article_id in drift.added:
        print(f"  present in the database but unlabelled: {article_id[:12]}")
    for article_id, fields in drift.changed:
        print(f"  changed {article_id[:12]}: {', '.join(fields)}")


def _render_metrics(metrics: Metrics) -> None:
    print(
        f"  tp {metrics.true_positives} | fp {metrics.false_positives}"
        f" | fn {metrics.false_negatives} | tn {metrics.true_negatives}"
    )
    print(f"  precision {metrics.precision:.3f} | recall {metrics.recall:.3f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    skeleton = subparsers.add_parser("worksheet", help="print a labelling skeleton from the corpus")
    skeleton.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    skeleton.add_argument("--ticker", default=DEFAULT_TICKER)
    skeleton.add_argument("--output", type=Path)

    scoring = subparsers.add_parser("evaluate", help="score the gate against the labelled gold set")
    scoring.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    scoring.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    scoring.add_argument(
        "--no-drift-check",
        action="store_true",
        help="score the fixture alone, without comparing it against the stored corpus",
    )

    arguments = parser.parse_args(argv)
    # Real headlines carry characters a legacy console encoding cannot represent, and a labelling
    # tool that dies on one of them is worse than one that escapes it.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    if arguments.command == "worksheet":
        skeleton_payload = worksheet(arguments.database, arguments.ticker)
        if arguments.output:
            arguments.output.write_text(
                json.dumps(skeleton_payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"wrote {arguments.output}")
        else:
            # Escaped on the way to a terminal, verbatim on the way to a file.
            print(json.dumps(skeleton_payload, indent=2, ensure_ascii=True))
        return 0

    database = None if arguments.no_drift_check else arguments.database
    if database is not None and not database.exists():
        print(f"drift check skipped: no database at {database}")
        database = None
    report = evaluate(arguments.fixture, database=database)
    render_report(report)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
