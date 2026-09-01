"""Pure presentation preparation for the read-only Relevant News browser.

Every row here already exists as a persisted ``ScoredArticle`` and, where present, a persisted
``ArticleAnalysis``. This module adds no judgement of its own -- it does not decide materiality,
does not rank or group, and never triggers a new analysis. "Analysed" means only that a
*currently version-compatible* stored analysis exists for that article: the exact same
``ArticleAnalysisCompatibility.accepts_for_display`` test the rest of the product already applies
to a stored analysis, never a re-derived or looser notion of "analysed".
"""

from collections.abc import Sequence
from dataclasses import dataclass

from marketsentinel.domain import ArticleAnalysis, ScoredArticle

RELEVANT_NEWS_CAPTION = (
    'Every stored, sentiment-scored article for this company. "Analysed" marks a stored event '
    "analysis under the current model and prompt versions -- informational only, never a "
    "triggerable action."
)
EMPTY_RELEVANT_NEWS_MESSAGE = "No stored articles are available for this company."


@dataclass(frozen=True)
class ArticleRow:
    """A display-ready row: one stored article plus its own compatibility flag."""

    article: ScoredArticle
    has_compatible_analysis: bool


def prepare_relevant_news(
    articles: Sequence[ScoredArticle],
    compatible_analyses: Sequence[ArticleAnalysis],
) -> list[ArticleRow]:
    """Pair each stored article with its compatibility flag. Order is preserved as supplied."""

    analysed_ids = {analysis.article_id for analysis in compatible_analyses}
    return [
        ArticleRow(article=article, has_compatible_analysis=article.fingerprint in analysed_ids)
        for article in articles
    ]
