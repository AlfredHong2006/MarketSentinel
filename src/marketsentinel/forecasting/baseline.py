"""Five-session direction baseline using only information available at each date."""

from collections.abc import Sequence

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from marketsentinel.domain import (
    DailySentiment,
    ForecastMetrics,
    ForecastResult,
    PricePoint,
)
from marketsentinel.errors import ForecastError

_PRICE_FEATURES = [
    "return_1d",
    "return_5d",
    "sma_5_ratio",
    "sma_20_ratio",
    "volatility_10d",
    "volume_5d_ratio",
]
_SENTIMENT_FEATURES = ["sentiment_score", "sentiment_ma_7d", "sentiment_article_count"]


class BaselineForecaster:
    """Chronologically evaluated logistic baseline, not a price-target model."""

    def __init__(
        self,
        horizon: int = 5,
        minimum_training_rows: int = 120,
        minimum_sentiment_days: int = 10,
    ) -> None:
        self.horizon = horizon
        self.minimum_training_rows = minimum_training_rows
        self.minimum_sentiment_days = minimum_sentiment_days

    def forecast(
        self,
        prices: Sequence[PricePoint],
        sentiment: Sequence[DailySentiment],
    ) -> ForecastResult:
        frame = _feature_frame(prices, sentiment, self.horizon)
        labelled = frame.dropna(subset=["target"]).copy()
        if len(labelled) < self.minimum_training_rows:
            raise ForecastError(
                f"Need at least {self.minimum_training_rows} usable observations; got {len(labelled)}"
            )
        if labelled["target"].nunique() < 2:
            raise ForecastError("Historical direction target contains only one class")

        sentiment_days = int((labelled["sentiment_article_count"] > 0).sum())
        use_sentiment = sentiment_days >= self.minimum_sentiment_days
        features = _PRICE_FEATURES + (_SENTIMENT_FEATURES if use_sentiment else [])

        validation_size = max(30, round(len(labelled) * 0.2))
        split_at = len(labelled) - validation_size
        # Purge one forecast horizon so training labels never use prices from validation dates.
        training = labelled.iloc[: split_at - self.horizon]
        validation = labelled.iloc[split_at:]
        if len(training) < 2:
            raise ForecastError(
                "Not enough observations remain after purging the validation boundary"
            )
        if training["target"].nunique() < 2:
            raise ForecastError("Chronological training window contains only one target class")

        evaluation_model = _model()
        evaluation_model.fit(training[features], training["target"].astype(int))
        predictions = evaluation_model.predict(validation[features])
        probabilities = evaluation_model.predict_proba(validation[features])[:, 1]
        actual = validation["target"].astype(int)

        majority_class = int(training["target"].mean() >= 0.5)
        majority_predictions = np.full(len(validation), majority_class)
        momentum_predictions = (validation["return_5d"] > 0).astype(int)
        roc_auc = float(roc_auc_score(actual, probabilities)) if actual.nunique() > 1 else None

        final_model = _model()
        final_model.fit(labelled[features], labelled["target"].astype(int))
        latest = frame.iloc[[-1]]
        probability_up = float(final_model.predict_proba(latest[features])[0, 1])
        coverage = sentiment_days / len(labelled)

        return ForecastResult(
            horizon_trading_days=self.horizon,
            probability_up=probability_up,
            as_of=latest.index[0].date(),
            trained_through=labelled.index[-1].date(),
            training_samples=len(labelled),
            sentiment_coverage=coverage,
            sentiment_features_used=use_sentiment,
            features=features,
            metrics=ForecastMetrics(
                validation_accuracy=float(accuracy_score(actual, predictions)),
                majority_baseline_accuracy=float(accuracy_score(actual, majority_predictions)),
                momentum_baseline_accuracy=float(accuracy_score(actual, momentum_predictions)),
                roc_auc=roc_auc,
                validation_samples=len(validation),
            ),
            warning=(
                "Experimental probability from a logistic baseline. It is not calibrated as an "
                "investment outcome and does not predict an exact future price."
            ),
        )


def _model() -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(max_iter=1_000, class_weight="balanced", random_state=42),
            ),
        ]
    )


def _feature_frame(
    prices: Sequence[PricePoint],
    sentiment: Sequence[DailySentiment],
    horizon: int,
) -> pd.DataFrame:
    if not prices:
        raise ForecastError("No price history is available")
    frame = pd.DataFrame(
        {
            "date": [point.date for point in prices],
            "close": [point.close for point in prices],
            "volume": [point.volume for point in prices],
        }
    ).set_index("date")
    frame.index = pd.to_datetime(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    daily_return = frame["close"].pct_change()
    frame["return_1d"] = daily_return
    frame["return_5d"] = frame["close"].pct_change(5)
    frame["sma_5_ratio"] = frame["close"] / frame["close"].rolling(5).mean() - 1
    frame["sma_20_ratio"] = frame["close"] / frame["close"].rolling(20).mean() - 1
    frame["volatility_10d"] = daily_return.rolling(10).std() * np.sqrt(252)
    volume_average = frame["volume"].rolling(5).mean().replace(0, np.nan)
    frame["volume_5d_ratio"] = frame["volume"] / volume_average - 1

    aligned_sentiment = _align_sentiment_to_next_session(frame.index, sentiment)
    if aligned_sentiment.empty:
        frame[_SENTIMENT_FEATURES] = 0.0
    else:
        frame = frame.join(aligned_sentiment, how="left")
        frame[_SENTIMENT_FEATURES] = frame[_SENTIMENT_FEATURES].fillna(0.0)

    future_return = frame["close"].shift(-horizon) / frame["close"] - 1
    frame["target"] = (future_return > 0).astype(float)
    frame.loc[future_return.isna(), "target"] = np.nan
    return frame.iloc[19:]


def _align_sentiment_to_next_session(
    price_index: pd.DatetimeIndex,
    sentiment: Sequence[DailySentiment],
) -> pd.DataFrame:
    """Conservatively make a calendar day's sentiment available next trading session."""

    mapped: list[dict[str, object]] = []
    for item in sentiment:
        published_date = pd.Timestamp(item.date)
        position = int(price_index.searchsorted(published_date, side="right"))
        if position >= len(price_index):
            continue
        mapped.append(
            {
                "date": price_index[position],
                "weighted_score": item.score * item.article_count,
                "sentiment_ma_7d": item.moving_average_7d,
                "sentiment_article_count": item.article_count,
            }
        )
    if not mapped:
        return pd.DataFrame(columns=_SENTIMENT_FEATURES, index=pd.DatetimeIndex([]))

    mapped_frame = pd.DataFrame(mapped).sort_values("date")
    grouped = mapped_frame.groupby("date", sort=True).agg(
        weighted_score=("weighted_score", "sum"),
        sentiment_ma_7d=("sentiment_ma_7d", "last"),
        sentiment_article_count=("sentiment_article_count", "sum"),
    )
    grouped["sentiment_score"] = grouped["weighted_score"] / grouped["sentiment_article_count"]
    return grouped[_SENTIMENT_FEATURES]
