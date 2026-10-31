# Reusable function to add volatility features e.g. log_return, realised volatility etc to the OHLCV data. 
# Be careful, it can add the target the volatility model predicts: target_vol_20d then drops NA values. Make sure it doesn't drop the current price observation

import numpy as np
import pandas as pd


def add_volatility_features(data: pd.DataFrame, horizon: int = 20, windows: tuple[int, ...] = (5, 10, 20, 60), range_window: int = 20, calculate_y: bool = True) -> pd.DataFrame:
    """
    Build volatility features and the future realised volatility label from OHLCV data.

    Features use only past/current observations. The label is the realised
    volatility over the next `horizon` days (computed from future returns).

    Args:
        data: DataFrame with "Close", "High", "Low" and "Open" columns.
        horizon: Look-ahead period (days) for the target label.
        windows: Rolling windows (days) for historical realised volatility (rv_{n}d).
        range_window: Rolling window (days) for Parkinson and Garman-Klass volatility.

    Returns:
        New DataFrame (input is not modified) with added feature columns and
        `target_vol_{horizon}d`, with NaN rows removed.

    Example:
        >>> df = add_volatility_features(raw_data)
        >>> df = add_volatility_features(raw_data, horizon=10, windows=(5, 20))
    """
    df = data.copy()

    # Daily returns
    r = np.log(df["Close"] / df["Close"].shift(1))
    r2 = r ** 2

    # Historical features (past/current observations only)
    df["log_return"] = r
    df["abs_return"] = np.abs(r)
    df["squared_return"] = r2

    for w in windows:
        squared_returns = pd.Series(r2, index=df.index)
        df[f"rv_{w}d"] = np.sqrt(squared_returns.rolling(w).sum())

    # Range-based variance features
    hl = np.log(df["High"] / df["Low"])
    co = np.log(df["Close"] / df["Open"])

    df["parkinson_var"] = hl**2 / (4 * np.log(2))
    df["gk_var"] = 0.5 * hl**2 - (2 * np.log(2) - 1) * co**2

    df[f"parkinson_vol_{range_window}d"] = np.sqrt(df["parkinson_var"].rolling(range_window).sum())
    df[f"gk_vol_{range_window}d"] = np.sqrt(df["gk_var"].rolling(range_window).sum().clip(lower=0))

    # Future realised volatility label (if calculate_y == True)
    if calculate_y == True:
        squared_returns = pd.Series(r2, index=df.index)
        df[f"target_vol_{horizon}d"] = np.sqrt(
            squared_returns.rolling(horizon).sum().shift(-horizon)
        )

    return df.dropna()