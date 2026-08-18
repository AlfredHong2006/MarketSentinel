"""One compatibility contract for persisted article-analysis interpretations."""

from dataclasses import dataclass

from marketsentinel.domain import ArticleAnalysis


@dataclass(frozen=True)
class ArticleAnalysisCompatibility:
    """Version fields that must agree before a stored analysis is displayed or reused."""

    model_version: str
    stage_a_prompt_version: str
    stage_b_prompt_version: str
    stage_c_prompt_version: str
    schema_version: str

    def accepts_for_cache(self, analysis: ArticleAnalysis, *, evidence_fingerprint: str) -> bool:
        """Whether a stored result can replace a new paid analysis request."""

        return (
            analysis.model_version == self.model_version
            and analysis.evidence_fingerprint == evidence_fingerprint
            and self.accepts_for_display(analysis)
        )

    def accepts_for_display(self, analysis: ArticleAnalysis) -> bool:
        """Whether the running application can safely interpret this typed payload."""

        return (
            analysis.stage_a_prompt_version == self.stage_a_prompt_version
            and analysis.stage_b_prompt_version == self.stage_b_prompt_version
            and analysis.stage_c_prompt_version == self.stage_c_prompt_version
            and analysis.schema_version == self.schema_version
        )
