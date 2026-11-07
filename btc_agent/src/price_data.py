import logging
import time
from collections.abc import Callable
from typing import TypeVar

import pandas as pd
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)

# Yahoo Finance sometimes rate-limits or briefly blocks requests, especially from cloud servers such as EC2. Each fetch is
# tried once, then retried after each of these waits (seconds) before giving up: 4 attempts over about a minute
RETRY_DELAYS_SECONDS = (5, 15, 45)

T = TypeVar("T")


def _alert_failure(what: str, error: Exception, attempts: int) -> None:
    """Send a Telegram alert that a Yahoo Finance fetch failed after all its retries. Never raises.

    notification.py is imported here rather than at the top of the module, because it requires TELEGRAM_BOT_TOKEN at import
    time and the frontend (which also uses this module) shouldn't need it. Without the token the alert is just logged as
    unsent. The error text comes from yfinance and contains no secrets.

    Example:
        >>> _alert_failure("BTC-USD daily history (6mo)", RuntimeError("Yahoo Finance returned no BTC-USD historical data."), 4)
    """
    message = f"BTC agent: Yahoo Finance request failed after {attempts} attempts.\nRequest: {what}\nError: {type(error).__name__}: {error}"
    try:
        from src.notification import chat_id, send_telegram_message
        send_telegram_message(chat_id, message)
    except Exception as alert_error: # e.g. TELEGRAM_BOT_TOKEN isn't set (the frontend run locally) or Telegram is unreachable; errors are already token-redacted
        logger.warning("Couldn't send the Telegram alert for the failed Yahoo Finance request: %s", alert_error)


def _with_retries(what: str, fetch: Callable[[], T], retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS) -> T:
    """Call ``fetch``, retrying after each wait in ``retry_delays`` if it raises. If every attempt fails, log it, send a
    Telegram alert (see _alert_failure) and raise RuntimeError, so callers fail exactly as they did without retries.

    Example:
        >>> _with_retries("BTC-USD live quote", _fetch_live_once)
    """
    for attempt, delay in enumerate((*retry_delays, None), start=1):
        try:
            return fetch()
        except Exception as error:
            if delay is None:
                logger.error("Yahoo Finance request for %s failed after %d attempts: %s", what, attempt, error)
                _alert_failure(what, error, attempt)
                raise RuntimeError(f"Yahoo Finance request for {what} failed after {attempt} attempts: {error}") from error
            logger.warning("Yahoo Finance request for %s failed (attempt %d), retrying in %ds: %s", what, attempt, delay, error)
            time.sleep(delay)
    raise AssertionError("unreachable")


def fetch_historical_BTCUSD(period: str = "max", retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS) -> pd.DataFrame:
    """Download daily BTC-USD history from Yahoo Finance, retrying if the request fails (see RETRY_DELAYS_SECONDS).

    Returns a DataFrame with a Date column and price columns. Raises RuntimeError (after sending a Telegram alert) if every
    attempt fails. ``retry_delays`` can be shortened where waiting a minute isn't acceptable, e.g. in the frontend.

    Examples:
        ``history = fetch_historical_BTCUSD()``  # the maximum available history
        ``recent_history = fetch_historical_BTCUSD(period="1y", retry_delays=(3,))``
    """
    return _with_retries(f"BTC-USD daily history ({period})", lambda: _fetch_historical_once(period), retry_delays)


def fetch_live_BTCUSD(retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS) -> pd.DataFrame:
    """Fetch the latest BTC-USD quote from Yahoo Finance, retrying if the request fails (see RETRY_DELAYS_SECONDS).

    Returns one row with QuoteTime, Close, High, Low, Open and Volume columns. Raises RuntimeError (after sending a Telegram
    alert) if every attempt fails.

    Example:
        ``live_btc = fetch_live_BTCUSD()``
    """
    return _with_retries("BTC-USD live quote", _fetch_live_once, retry_delays)


# Single attempts, without retries (use the public functions above):

def _fetch_historical_once(period: str = "max") -> pd.DataFrame:
    """Download daily BTC-USD history in a single attempt (no retries; use fetch_historical_BTCUSD)

    Downloads data from Yahoo Finance, flattens multi-index columns if needed,
    and returns a DataFrame with a date column and price columns.

    Args:
        period: Yahoo Finance lookback period, such as ``"1y"`` or ``"max"``.

    Returns:
        The downloaded BTC-USD history as a DataFrame.

    Raises:
        RuntimeError: If Yahoo Finance returns no historical data.

    Examples:
        Download the maximum available history:
        ``history = _fetch_historical_once()``

        Download one year of history without saving:
        ``recent_history = _fetch_historical_once(period="1y")``
    """
    data = yf.download("BTC-USD", period=period, interval="1d", progress=False) # No progress bar: it would clutter the logs

    if data is None:
        raise RuntimeError("Yahoo Finance returned no BTC-USD historical data.")

    if not isinstance(data, pd.DataFrame):
        raise RuntimeError("Yahoo Finance returned unexpected data for BTC-USD historical data.")

    if data.empty:
        raise RuntimeError("Yahoo Finance returned no BTC-USD historical data.")

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.reset_index()
    logger.debug("BTC-USD history memory usage: %.2f MiB", data.memory_usage(deep=True).sum() / 1024**2)

    return data

def _fetch_live_once() -> pd.DataFrame:
    """Fetch the latest available BTC-USD quote or intraday bar from Yahoo Finance in a single attempt (no retries).

    Returns one row with ``QuoteTime``, ``Close``, ``High``, ``Low``,
    ``Open``, and ``Volume`` columns. If the fast quote is unavailable, the
    latest 1-minute bar is used; that bar may not be current.

    Example:
        ``live_btc = _fetch_live_once()``
    """
    ticker = yf.Ticker("BTC-USD")
    quote_time = None

    try:
        quote = ticker.fast_info
        price = quote.get("last_price")
        for key in ("last_trade_time", "last_update", "last_updated", "timestamp"):
            value = quote.get(key)
            if value is not None and not pd.isna(value):
                quote_time = value
                break
    except Exception:
        quote = {}
        price = None

    bar = None
    if price is None or pd.isna(price):
        intraday = ticker.history(period="1d", interval="1m")
        if not intraday.empty:
            bar = intraday.iloc[-1]
            price = bar.get("Close")
            if quote_time is None:
                quote_time = bar.name

    if price is None or pd.isna(price):
        raise RuntimeError(
            "Yahoo Finance returned no BTC-USD quote or intraday price. Try again later."
        )

    if quote_time is None:
        quote_time = pd.Timestamp.now(tz="UTC").tz_localize(None)

    def normalize_ts(ts):
        if ts is None or pd.isna(ts):
            return None
        try:
            if isinstance(ts, (int, float, np.integer, np.floating)):
                return pd.to_datetime(ts, unit="s", utc=True).tz_localize(None)
            timestamp = pd.Timestamp(ts)
            if timestamp.tzinfo is not None:
                return timestamp.tz_convert("UTC").tz_localize(None)
            return timestamp
        except Exception:
            return None

    quote_time = normalize_ts(quote_time)
    if quote_time is None:
        quote_time = pd.Timestamp.now(tz="UTC").tz_localize(None)

    def quote_or_bar(key: str, quote_key: str) -> float:
        value = quote.get(quote_key, np.nan)
        if value is None or pd.isna(value):
            value = bar.get(key, np.nan) if bar is not None else np.nan
        return value

    # Sometimes fetching latest price returns volume = 0 (or none at all). This would mess up the predictions. Fix:
    volume = quote_or_bar("Volume", "last_volume")
    if pd.isna(volume) or volume == 0:
        try:
            last_day = _fetch_historical_once("5d").tail(1) # Gather latest data row (a single attempt: the price matters more than the volume)
            volume = int(last_day["Volume"].iloc[-1])
        except Exception:
            logger.warning("Couldn't fetch a replacement volume for the live quote; leaving it as %s.", volume)

    return pd.DataFrame([{
        "QuoteTime": quote_time,
        "Close": price,
        "High": quote_or_bar("High", "day_high"),
        "Low": quote_or_bar("Low", "day_low"),
        "Open": quote_or_bar("Open", "open"),
        "Volume": volume,
    }])



