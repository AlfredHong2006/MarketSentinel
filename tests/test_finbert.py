import pytest

from marketsentinel.errors import SentimentModelError
from marketsentinel.sentiment.finbert import label_indices


def test_finbert_label_mapping_uses_model_config_order() -> None:
    mapping = label_indices({0: "positive", 1: "negative", 2: "neutral"})

    assert mapping == {"positive": 0, "negative": 1, "neutral": 2}
    probabilities = [0.7, 0.1, 0.2]
    score = probabilities[mapping["positive"]] - probabilities[mapping["negative"]]
    assert score == pytest.approx(0.6)


def test_finbert_label_mapping_rejects_unknown_config() -> None:
    with pytest.raises(SentimentModelError, match="missing"):
        label_indices({0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"})
