"""Opt-in synthetic integration smoke test for the three-stage article-intelligence provider.

Run: uv run python scripts/smoke_event_intelligence.py
The script never reads stored articles and never prints credentials, prompts, or model text.
"""

import sys
import time
from datetime import UTC, datetime

from marketsentinel.config import Settings
from marketsentinel.domain import (
    ArticleEvidenceReference,
    ClaimAssessments,
    CompanyReference,
    SourceClass,
)
from marketsentinel.event_analysis import (
    ClaimAssessmentRequest,
    EventExtractionRequest,
    OpenAIArticleIntelligenceProvider,
    RelatedCompanyRequest,
)


def report(stage: str, started: float, provider: OpenAIArticleIntelligenceProvider) -> None:
    input_tokens, output_tokens = provider.last_usage.get(stage, (None, None))
    print(
        f"stage={stage} provider_success=true latency_ms={round((time.perf_counter() - started) * 1_000)} "
        f"pydantic_validation=success input_tokens={input_tokens} output_tokens={output_tokens}"
    )


def main() -> int:
    settings = Settings()
    if not settings.llm_api_key:
        print("MARKETSENTINEL_LLM_API_KEY is unavailable; no API request was made.")
        return 2

    provider = OpenAIArticleIntelligenceProvider(
        api_key=settings.llm_api_key,
        model_version=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    subject = CompanyReference(symbol="NVDA", name="NVIDIA")
    article = ArticleEvidenceReference(
        article_id="synthetic-nvda-article",
        title="NVIDIA announced a new data-centre GPU.",
        publisher="Synthetic Financial Wire",
        published_at=datetime.now(UTC),
        url="https://example.invalid/synthetic-nvda",
    )
    try:
        started = time.perf_counter()
        event = provider.extract_event(
            EventExtractionRequest(
                subject_company=subject,
                article=article,
                snippet="Synthetic record only: NVIDIA announced a new data-centre GPU.",
                source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
            )
        )
        report("stage_a", started, provider)

        started = time.perf_counter()
        assessments = provider.assess_claims(
            ClaimAssessmentRequest(
                claims=(("claim_1", "NVIDIA announced a new data-centre GPU."),),
                evidence=(
                    (
                        ArticleEvidenceReference(
                            article_id="synthetic-evidence-1",
                            title="Independent synthetic record confirms the GPU announcement.",
                            publisher="Synthetic Independent Wire",
                            published_at=datetime.now(UTC),
                            url="https://example.invalid/synthetic-evidence",
                        ),
                        "Synthetic independent confirmation record.",
                        SourceClass.MAJOR_FINANCIAL_NEWS,
                    ),
                ),
            )
        )
        if not isinstance(assessments, ClaimAssessments):
            raise TypeError("Stage B did not return a typed assessment.")
        report("stage_b", started, provider)

        started = time.perf_counter()
        related = provider.select_related_companies(
            RelatedCompanyRequest(
                subject_company=subject,
                event=event,
                candidates=(
                    CompanyReference(symbol="AMD", name="AMD"),
                    CompanyReference(symbol="AVGO", name="Broadcom"),
                ),
            )
        )
        if any(item.ticker.upper() not in {"AMD", "AVGO"} for item in related.related_companies):
            raise ValueError("Stage C returned a ticker outside the supplied candidate set.")
        report("stage_c", started, provider)
    except Exception as exc:
        print(f"provider_success=false error_type={type(exc).__name__}")
        return 1

    print("candidate_universe_validation=success subject_company_excluded=success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
