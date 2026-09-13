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

    @property
    def contract_key(self) -> str:
        """Stable identity of the analysis contract, deliberately without evidence.

        Keys the analysis job ledger: one article is analysed once per contract. A model, prompt,
        or schema change yields a different key and therefore a fresh, explicit set of jobs.
        """

        return (
            f"m={self.model_version};a={self.stage_a_prompt_version};"
            f"b={self.stage_b_prompt_version};c={self.stage_c_prompt_version};"
            f"s={self.schema_version}"
        )

    def accepts_for_contract(self, analysis: ArticleAnalysis) -> bool:
        """Whether a stored result completes an article's analysis job under this contract.

        Display compatibility plus a matching model version, *without* the evidence fingerprint.
        This is a spending rule, not a cache rule: it lets the job ledger avoid paying again when
        only the evidence pool has grown since the analysis was produced. It never replaces
        ``accepts_for_cache``, so an explicit evidence refresh still regenerates, and the ledger
        records separately whether the stored evidence is still current.
        """

        return analysis.model_version == self.model_version and self.accepts_for_display(analysis)

    def accepts_for_display(self, analysis: ArticleAnalysis) -> bool:
        """Whether the running application can safely interpret this typed payload."""

        return (
            analysis.stage_a_prompt_version == self.stage_a_prompt_version
            and analysis.stage_b_prompt_version == self.stage_b_prompt_version
            and analysis.stage_c_prompt_version == self.stage_c_prompt_version
            and analysis.schema_version == self.schema_version
        )
