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


def predict_volatility(volatility_data: pd.DataFrame, model_path: Union[str, Path], preprocessor_path: Union[str, Path], as_of_date: str) -> dict:
    """Run the volatility model and return its result as a dict the agent can read. Not implemented until the model is trained.

    Return the same shape as predict_direction so the agent sees both models consistently: "status": "ok", the model's
    name, target, test metrics and known limitations, "as_of_date", "inputs", and the prediction itself
    (e.g. "predicted_volatility" with its units and horizon).

    Example:
        >>> predict_volatility(volatility_data, VOLATILITY_MODEL_DIR / "model.keras", VOLATILITY_MODEL_DIR / "pipeline.joblib", "2026-11-02")
        {'status': 'ok', 'predicted_volatility': ..., ...}
    """
    raise NotImplementedError("Volatility inference hasn't been written yet")


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