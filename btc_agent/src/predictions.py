# Make price direction and volatility directions using the trained models

import joblib
from pathlib import Path
from typing import Any, Union

import pandas as pd

from tensorflow import keras
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils.validation import check_is_fitted


def load_preprocessor(path: Union[str, Path]) -> Any:
      """Load a serialized preprocessing object from a file.

      Args:
            path: Path to the serialized preprocessor.

      Returns:
            The loaded preprocessing object.
      """
      
      return joblib.load(path)

def load_keras_model(path: Union[str, Path]) -> Any:
      """Load a serialized Keras model from a .keras file.

      Args:
            path: Path to the .keras model file.

      Returns:
            The loaded Keras model.

      Raises:
            FileNotFoundError: If the model file does not exist.
      """

      model_path = Path(path)
      if not model_path.is_file():
            raise FileNotFoundError(f"Keras model not found: {model_path}")
      return keras.models.load_model(model_path)


# Model details shown to the agent alongside each prediction, so it can judge how far to trust it.
# Test metrics are from the held-out test set in the direction modelling notebook (model_simple_eval.png).
DIRECTION_MODEL_INFO = {
    "model": "LSTM (model_simple)",
    "target": "Probability that BTC's daily close is higher 20 trading days after the latest completed day",
    "test_metrics": {"roc_auc": 0.611, "accuracy": 0.545, "share_of_actual_ups_predicted_up": 0.198, "base_rate_up": 0.508},
    "known_limitations": "Trained only on raw daily closing prices, mostly from years when BTC was far cheaper. Prices outside that range push its output towards a near-constant value, and on the test set it predicted 'down' about 85% of the time. Treat it as a weak signal at best.",
}


def predict_direction(direction_data: pd.DataFrame, model_path: Union[str, Path], preprocessor_path: Union[str, Path], as_of_date: str) -> dict:
    """Run the direction model on completed daily closes and return the result as a dict the agent can read.

    ``direction_data`` is a one-column DataFrame of completed daily closes, oldest first ('Close', the column the scaler was
    fitted on). The last ``lookback`` closes (48 for model_simple) go through the saved pipeline (scaler + rolling window)
    and the LSTM. Also reports how far the latest close is from the prices the scaler was fitted on, since the model is
    unreliable far outside that range.

    Example:
        >>> predict_direction(history[["Close"]], DIRECTION_MODEL_DIR / "model_simple.keras", DIRECTION_MODEL_DIR / "process_pipeline.joblib", "2026-11-02")
        {'status': 'ok', 'probability_up': 0.2451, 'predicted_direction': 'down', ...}
    """
    pipeline = load_preprocessor(preprocessor_path)
    model = load_keras_model(model_path)
    lookback = pipeline.named_steps["rolling"].lookback

    if len(direction_data) < lookback:
        raise ValueError(f"The direction model needs {lookback} completed daily closes but only {len(direction_data)} were supplied")
    closes = direction_data[["Close"]].tail(lookback) # Keep it a DataFrame: the scaler was fitted on a 'Close' column

    probability_up = float(model.predict(pipeline.transform(closes), verbose=0)[0][0])
    scaler = pipeline.named_steps["scaler"]
    distance = float(scaler.transform(closes.tail(1))[0][0]) # Standard deviations from the mean close in the training data

    result = {
        "status": "ok",
        **DIRECTION_MODEL_INFO,
        "as_of_date": as_of_date,
        "inputs": f"Last {lookback} completed daily closes",
        "probability_up": round(probability_up, 4),
        "predicted_direction": "up" if probability_up >= 0.5 else "down",
        "decision_threshold": 0.5,
        "latest_close_vs_training_data": f"{distance:.1f} standard deviations from the training mean close of ${scaler.mean_[0]:,.0f}",
    }
    if abs(distance) > 3:
        result["warning"] = "The latest close is far outside the prices this model was trained on, so this prediction is especially unreliable."
    return result


# Model details shown to the agent alongside each volatility prediction. Test metrics are read from the evaluation JSON saved
# next to the model by the volatility modelling notebook (see load_volatility_metrics), so they stay correct after retraining.
VOLATILITY_HORIZON_DAYS = 20 # The target is realised volatility over the next 20 daily candles (BTC trades every day)
DAYS_PER_YEAR = 365 # For annualising: BTC trades every day of the year
VOLATILITY_MODEL_INFO = {
    "model": "LSTM (vol_model_simple)",
    "target": f"Realised volatility of BTC's daily log returns over the next {VOLATILITY_HORIZON_DAYS} days, i.e. sqrt(sum of squared daily log returns), not annualised",
    "known_limitations": "On its test set it was less accurate than simply assuming the last 20 days' realised volatility carries on (the persistence baseline below): higher error, and its predictions were not positively correlated with what happened. Prefer the persistence baseline, and treat the model's figure as a weak second opinion at best.",
}


def load_volatility_metrics(evaluation_path: Union[str, Path]) -> dict:
    """Read the volatility model's test metrics (and the persistence baseline's) from the notebook's evaluation JSON.

    Returns an empty dict if the file is missing or unreadable, so a missing evaluation file never stops a prediction.

    Example:
        >>> load_volatility_metrics(VOLATILITY_MODEL_DIR / "vol_model_evaluation.json")
        {'model': {'MAE': 0.052, ...}, 'persistence_baseline': {'MAE': 0.0346, ...}}
    """
    try:
        evaluation = pd.read_json(evaluation_path, orient="index")
    except (OSError, ValueError):
        return {}
    rounded = evaluation.round(4)
    names = {"LSTM": "model", "Persistence": "persistence_baseline"}
    return {names.get(name, name): rounded.loc[name].to_dict() for name in rounded.index}


def predict_volatility(volatility_data: pd.DataFrame, model_path: Union[str, Path], preprocessor_path: Union[str, Path], as_of_date: str, evaluation_path: Union[str, Path, None] = None) -> dict:
    """Run the volatility model on completed daily candles and return the result as a dict the agent can read.

    ``volatility_data`` is completed daily OHLCV with the features from add_volatility_features(..., calculate_y=False),
    oldest first. The last ``lookback`` rows (48 for vol_model_simple) of the columns the scaler was fitted on go through
    the saved pipeline (scaler + rolling window) and the LSTM. The latest rv_20d is reported as the persistence baseline,
    since it beat the model on the test set. Volatilities are given per 20 days and annualised.

    Example:
        >>> features = add_volatility_features(completed[["Open", "High", "Low", "Close", "Volume"]], calculate_y=False)
        >>> predict_volatility(features, VOLATILITY_MODEL_DIR / "vol_model_simple.keras", VOLATILITY_MODEL_DIR / "vol_pipeline.joblib", "2026-11-02")
        {'status': 'ok', 'predicted_volatility_20d': 0.0712, 'persistence_baseline_20d': 0.0654, ...}
    """
    pipeline = load_preprocessor(preprocessor_path)
    model = load_keras_model(model_path)
    lookback = pipeline.named_steps["rolling"].lookback
    scaler = pipeline.named_steps["scaler"]
    feature_columns = list(scaler.feature_names_in_) # Same columns, in the same order, as in training

    missing = [column for column in feature_columns if column not in volatility_data.columns]
    if missing:
        raise ValueError(f"The volatility data is missing the feature columns {missing}")
    if len(volatility_data) < lookback:
        raise ValueError(f"The volatility model needs {lookback} days of features but only {len(volatility_data)} were supplied (the 60-day realised volatility needs about 60 extra days of history first)")
    features = volatility_data[feature_columns].tail(lookback)

    predicted = float(model.predict(pipeline.transform(features), verbose=0)[0][0])
    persistence = float(features["rv_20d"].iloc[-1])
    annualise = (DAYS_PER_YEAR / VOLATILITY_HORIZON_DAYS) ** 0.5
    close_index = feature_columns.index("Close")
    distance = float(scaler.transform(features.tail(1))[0][close_index]) # Scaled close: (close - training median) / training IQR

    result = {
        "status": "ok",
        **VOLATILITY_MODEL_INFO,
        "test_metrics": load_volatility_metrics(evaluation_path) if evaluation_path else {},
        "as_of_date": as_of_date,
        "inputs": f"Last {lookback} completed daily candles: OHLCV plus return and realised/range volatility features",
        "predicted_volatility_20d": round(predicted, 4),
        "predicted_volatility_annualised": round(predicted * annualise, 4),
        "persistence_baseline_20d": round(persistence, 4),
        "persistence_baseline_annualised": round(persistence * annualise, 4),
        "persistence_baseline_explained": "Realised volatility over the last 20 completed days, assumed to carry on. It beat the model on the test set.",
        "how_to_read": f"0.06 over 20 days means a typical 20-day move of about 6% (one standard deviation of the {VOLATILITY_HORIZON_DAYS}-day log return); annualised multiplies by sqrt({DAYS_PER_YEAR}/{VOLATILITY_HORIZON_DAYS}).",
        "latest_close_vs_training_data": f"{distance:.1f} interquartile ranges from the training median close of ${scaler.center_[close_index]:,.0f}",
    }
    if abs(distance) > 3:
        result["warning"] = "The latest close (and volume) is far outside the range this model was trained on, so its prediction is especially unreliable; rely on the persistence baseline."
    return result


# Custom transformers used in pipeline(s):

# Function to generate lag features in a pipeline:
class RollingWindowTransformer(BaseEstimator, TransformerMixin):
    def __init__(self, lookback=96):
        self.lookback = lookback

    def fit(self, X, y=None):
        X = np.asarray(X)
        self.n_features_in_ = X.shape[1]
        return self

    def transform(self, X):
        check_is_fitted(self)

        X = np.asarray(X, dtype=np.float32)

        if X.ndim != 2:
            raise ValueError("X must be 2D")

        if X.shape[1] != self.n_features_in_:
            raise ValueError("Number of features has changed")

        if not 1 <= self.lookback <= len(X):
            raise ValueError("Invalid lookback")

        return np.stack([
            X[i:i + self.lookback]
            for i in range(len(X) - self.lookback + 1)
        ])