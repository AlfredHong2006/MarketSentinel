"""Lazy, batched FinBERT inference with model-config-driven label mapping."""

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from marketsentinel.domain import Article, ScoredArticle
from marketsentinel.errors import SentimentModelError
from marketsentinel.timeutils import utc_now

LOGGER = logging.getLogger(__name__)
_EXPECTED_LABELS = {"positive", "negative", "neutral"}


class SentimentAnalyzer(Protocol):
    model_name: str

    def score(self, articles: Sequence[Article]) -> list[ScoredArticle]: ...


def label_indices(id2label: Mapping[int | str, str]) -> dict[str, int]:
    """Map semantic labels to logits indices using the model configuration."""

    result: dict[str, int] = {}
    for raw_index, raw_label in id2label.items():
        normalized = re.sub(r"[^a-z]", "", str(raw_label).casefold())
        for expected in _EXPECTED_LABELS:
            if normalized == expected or normalized.endswith(expected):
                result[expected] = int(raw_index)
                break
    missing = _EXPECTED_LABELS - result.keys()
    if missing:
        raise SentimentModelError(
            f"Model label mapping is missing {sorted(missing)}; received {dict(id2label)}"
        )
    return result


class FinBertAnalyzer:
    """Load ProsusAI/finbert once per service instance, only on first inference."""

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        device: str = "auto",
        batch_size: int = 8,
        hf_token: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.batch_size = batch_size
        self.hf_token = hf_token
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._device: str | None = None
        self._indices: dict[str, int] | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            device = self.requested_device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cuda" and not torch.cuda.is_available():
                raise SentimentModelError("CUDA was requested but is not available")

            tokenizer = AutoTokenizer.from_pretrained(self.model_name, token=self.hf_token)
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                token=self.hf_token,
            )
            indices = label_indices(model.config.id2label)
            model.to(device)
            model.eval()
        except SentimentModelError:
            raise
        except Exception as exc:
            LOGGER.exception("Unable to load FinBERT model %s", self.model_name)
            raise SentimentModelError(
                f"Unable to load {self.model_name}. Check model access, disk space, and network."
            ) from exc

        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._indices = indices
        self._device = device

    def score(self, articles: Sequence[Article]) -> list[ScoredArticle]:
        if not articles:
            return []
        self._load()
        assert self._torch is not None
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._indices is not None
        assert self._device is not None

        scored_at = utc_now()
        results: list[ScoredArticle] = []
        for offset in range(0, len(articles), self.batch_size):
            batch = articles[offset : offset + self.batch_size]
            encoded = self._tokenizer(
                [article.title for article in batch],
                padding=True,
                truncation=True,
                max_length=256,
                return_tensors="pt",
            )
            encoded = {key: value.to(self._device) for key, value in encoded.items()}
            with self._torch.inference_mode():
                logits = self._model(**encoded).logits
                probabilities = self._torch.softmax(logits, dim=-1).detach().cpu().tolist()

            for article, row in zip(batch, probabilities, strict=True):
                values = {label: float(row[index]) for label, index in self._indices.items()}
                label = max(values, key=values.__getitem__)
                results.append(
                    ScoredArticle(
                        **article.model_dump(),
                        label=label,
                        positive=values["positive"],
                        negative=values["negative"],
                        neutral=values["neutral"],
                        sentiment_score=values["positive"] - values["negative"],
                        model_name=self.model_name,
                        scored_at=scored_at,
                    )
                )
        return results


class StaticSentimentAnalyzer:
    """Simple injected analyzer for tests; never wired into the production app."""

    model_name = "test-static"

    def __init__(self, positive: float = 0.6, negative: float = 0.1, neutral: float = 0.3):
        self.values = {"positive": positive, "negative": negative, "neutral": neutral}

    def score(self, articles: Sequence[Article]) -> list[ScoredArticle]:
        scored_at = datetime.now().astimezone()
        label = max(self.values, key=self.values.__getitem__)
        return [
            ScoredArticle(
                **article.model_dump(),
                label=label,
                sentiment_score=self.values["positive"] - self.values["negative"],
                model_name=self.model_name,
                scored_at=scored_at,
                **self.values,
            )
            for article in articles
        ]
