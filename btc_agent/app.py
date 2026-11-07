# Front-end. Landing page for the user: live portfolio value, a daily BTC-USD candlestick chart with indicators,
# the open positions and trade history from trading.db, and the latest news. Run from this directory with: streamlit run app.py

# Base imports
import logging

import pandas as pd
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

# Custom imports
from src.price_data import fetch_live_BTCUSD, fetch_historical_BTCUSD
from src.price_indicators import add_SMA, add_EMA, add_RSI, add_MACD
from src.news import fetch_news_articles
from src.database.database import get_engine, fetch_table_data, portfolio_table, trades_table, cash_table
from src.logger import setup_logging

setup_logging() # Safe on every Streamlit rerun: handlers are only added once
logger = logging.getLogger(__name__)

PERIODS = ["3mo", "6mo", "1y", "2y", "5y", "max"] # Yahoo Finance lookback periods offered for the chart
APP_RETRY_DELAYS = (3,) # One quick retry: the page is blocked while it waits, so don't use price_data's minute-long default here
LIVE_PRICE_TTL_SECONDS = 300 # A price fetched less than 5 minutes ago is reused by page loads; the Refresh price button always fetches a new one
OVERLAYS = {"SMA_7": "#f59e0b", "SMA_20": "#ef4444", "EMA_7": "#3b82f6", "EMA_20": "#8b5cf6"} # Moving averages drawn on the price pane, and their colours
UP, DOWN = "#26a69a", "#ef5350" # Candle / histogram colours for rising and falling values


@st.cache_data(ttl=3600, show_spinner="Loading BTC-USD history...")
def load_history(period: str) -> pd.DataFrame:
    """Return daily BTC-USD history for ``period`` with the same indicators the agent sees (SMA, EMA, RSI, MACD).

    Cached for an hour, as daily candles only change once a day (apart from today's forming candle).

    Example:
        >>> history = load_history("1y")
        >>> history[["Date", "Close", "RSI_14"]].tail()
    """
    try:
        data = fetch_historical_BTCUSD(period=period, retry_delays=APP_RETRY_DELAYS)
    except Exception:
        logger.exception("Couldn't load BTC-USD history for the chart (period %s).", period)
        raise
    for n in (7, 20):
        data = add_EMA(data=add_SMA(data=data, n=n), n=n)
    return add_MACD(data=add_RSI(data=data))


@st.cache_data(ttl=LIVE_PRICE_TTL_SECONDS, show_spinner="Fetching the BTC-USD price...")
def load_live_price() -> tuple[float, pd.Timestamp]:
    """Return the latest BTC-USD price and when it was fetched (UTC).

    There's no automatic refresh, so Yahoo Finance is only asked when someone uses the app: a page load reuses a price fetched
    in the last LIVE_PRICE_TTL_SECONDS, and the Refresh price button clears this cache to force a new fetch. Fewer requests
    make Yahoo less likely to rate-limit the server.

    Example:
        >>> price, fetched_at = load_live_price()
    """
    try:
        return float(fetch_live_BTCUSD(retry_delays=APP_RETRY_DELAYS)["Close"].iloc[0]), pd.Timestamp.now(tz="UTC")
    except Exception:
        logger.exception("Couldn't load the live BTC-USD price.")
        raise


@st.cache_data(ttl=1800, show_spinner="Loading news...")
def load_news() -> list[dict] | str:
    """Return the latest articles from NewsAPI, or a message explaining why none could be loaded.

    Cached for 30 minutes to stay well inside the NewsAPI free tier's daily request limit.

    Example:
        >>> news = load_news()
        >>> news[0]["title"] if isinstance(news, list) else news
    """
    try:
        return fetch_news_articles() # SENSITIVE: reads the NEWS_API_KEY environment variable
    except SystemExit: # fetch_news_articles exits when NEWS_API_KEY isn't set
        return "NEWS_API_KEY is not set, so news can't be loaded."


def load_portfolio() -> tuple[pd.DataFrame, pd.DataFrame, float | None]:
    """Query trading.db for the open positions, the trade history (newest first), and the cash balance (None if never set).

    Not cached, so the tables always match the database after the agent trades.

    Example:
        >>> positions, trades, cash = load_portfolio()
    """
    engine = get_engine()
    positions = pd.DataFrame(fetch_table_data(engine, portfolio_table), columns=list(portfolio_table.c.keys()))
    trades = pd.DataFrame(fetch_table_data(engine, trades_table), columns=list(trades_table.c.keys()))
    cash_rows = fetch_table_data(engine, cash_table)
    if not trades.empty:
        trades = trades.sort_values("date_time", ascending=False)
    return positions, trades, cash_rows[0]["cash"] if cash_rows else None


def series_data(data: pd.DataFrame, column: str) -> list[dict]:
    """Convert one column of ``data`` into lightweight-charts points ({"time": "YYYY-MM-DD", "value": x}), skipping missing values.

    Indicators are missing for their warm-up period (e.g. the first 19 days of SMA_20), which lightweight-charts can't draw.

    Example:
        >>> series_data(history, "EMA_20")[-1]
        {'time': '2026-10-21', 'value': 82531.5}
    """
    rows = data[["Date", column]].dropna()
    return [{"time": date.strftime("%Y-%m-%d"), "value": float(value)} for date, value in zip(rows["Date"], rows[column])]


def build_charts(data: pd.DataFrame, overlays: list[str], dark: bool) -> list[dict]:
    """Build the three stacked, time-synced chart panes: candles + volume + moving averages, MACD, and RSI.

    Example:
        >>> renderLightweightCharts(build_charts(history, ["EMA_20"], dark=False), key="btc")
    """
    text, grid = ("#d1d4dc", "rgba(255,255,255,0.06)") if dark else ("#191919", "rgba(0,0,0,0.06)")
    def pane(height: int, show_time: bool) -> dict:
        return {"height": height, "layout": {"background": {"type": "solid", "color": "transparent"}, "textColor": text}, "grid": {"vertLines": {"color": grid}, "horzLines": {"color": grid}}, "rightPriceScale": {"borderVisible": False}, "timeScale": {"borderVisible": False, "visible": show_time}, "crosshair": {"mode": 0}}

    dates = data["Date"].dt.strftime("%Y-%m-%d")
    candles = [{"time": t, "open": o, "high": h, "low": l, "close": c} for t, o, h, l, c in zip(dates, data["Open"], data["High"], data["Low"], data["Close"])]
    volume = [{"time": t, "value": float(v), "color": UP + "66" if c >= o else DOWN + "66"} for t, v, o, c in zip(dates, data["Volume"], data["Open"], data["Close"])]
    price_series = [
        {"type": "Candlestick", "data": candles, "options": {"upColor": UP, "downColor": DOWN, "borderVisible": False, "wickUpColor": UP, "wickDownColor": DOWN}},
        {"type": "Histogram", "data": volume, "options": {"priceFormat": {"type": "volume"}, "priceScaleId": "volume", "lastValueVisible": False, "priceLineVisible": False}, "priceScale": {"scaleMargins": {"top": 0.8, "bottom": 0}}},
    ]
    price_series += [{"type": "Line", "data": series_data(data, name), "options": {"color": OVERLAYS[name], "lineWidth": 1.5, "title": name, "priceLineVisible": False, "lastValueVisible": False}} for name in overlays]

    macd_hist = [{"time": point["time"], "value": point["value"], "color": UP if point["value"] >= 0 else DOWN} for point in series_data(data, "MACD_hist")]
    macd_series = [
        {"type": "Histogram", "data": macd_hist, "options": {"title": "MACD hist", "priceLineVisible": False, "lastValueVisible": False}},
        {"type": "Line", "data": series_data(data, "MACD"), "options": {"color": "#3b82f6", "lineWidth": 1.5, "title": "MACD", "priceLineVisible": False}},
        {"type": "Line", "data": series_data(data, "MACD_signal"), "options": {"color": "#f59e0b", "lineWidth": 1.5, "title": "Signal", "priceLineVisible": False}},
    ]

    rsi = series_data(data, "RSI_14")
    band = lambda level: [{"time": point["time"], "value": level} for point in rsi] # Constant 70 / 30 overbought and oversold guides
    rsi_series = [
        {"type": "Line", "data": rsi, "options": {"color": "#8b5cf6", "lineWidth": 1.5, "title": "RSI 14", "priceLineVisible": False}},
        {"type": "Line", "data": band(70), "options": {"color": DOWN, "lineWidth": 1, "lineStyle": 2, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
        {"type": "Line", "data": band(30), "options": {"color": UP, "lineWidth": 1, "lineStyle": 2, "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False}},
    ]
    return [{"chart": pane(420, False), "series": price_series}, {"chart": pane(140, False), "series": macd_series}, {"chart": pane(140, True), "series": rsi_series}]


def portfolio_summary() -> None:
    """Show the live portfolio value (cash + open BTC at the latest price) and its parts, with a button to refresh the price.

    Positions are valued at the live price here, rather than the hourly mark stored in trading.db by main.py. Nothing
    refreshes on its own: the price is fetched when the page loads (see load_live_price) or the button is pressed.

    Example:
        >>> portfolio_summary()  # inside the Streamlit script
    """
    button_col, time_col = st.columns([1, 5], vertical_alignment="center")
    if button_col.button("Refresh price", icon=":material/refresh:"):
        load_live_price.clear() # The click reruns the script, so the next line fetches a new price
    try:
        price, fetched_at = load_live_price()
    except Exception: # Already logged (and alerted on Telegram) by load_live_price / price_data; failures aren't cached, so the button retries
        time_col.error("Couldn't get the BTC price from Yahoo Finance (it may be rate-limiting this server). Try Refresh price in a minute.")
        return
    time_col.caption(f"Price fetched at {fetched_at:%d %b %Y, %H:%M:%S} UTC")
    positions, trades, cash = load_portfolio()
    btc_held = float(positions["btc_amnt"].sum()) if not positions.empty else 0.0
    unrealised = float(((price - positions["open_price"]) * positions["btc_amnt"]).sum()) if not positions.empty else 0.0
    realised = float(trades["realized_pnl"].sum()) if not trades.empty else 0.0

    value, btc, cash_col, unreal_col, real_col = st.columns(5)
    value.metric("Portfolio value", f"${(cash or 0.0) + btc_held * price:,.2f}", border=True)
    btc.metric("BTC-USD", f"${price:,.2f}", border=True)
    cash_col.metric("Cash", f"${cash:,.2f}" if cash is not None else "Not set", border=True)
    unreal_col.metric("Unrealised P&L", f"${unrealised:,.2f}", f"{btc_held:g} BTC held", delta_color="off", delta_arrow="off", border=True)
    real_col.metric("Realised P&L", f"${realised:,.2f}", f"{len(trades)} trades", delta_color="off", delta_arrow="off", border=True)
    if cash is None:
        st.caption("No cash balance has been recorded yet, so the agent can't buy. Seed a starting balance to begin trading.")


def news_feed() -> None:
    """List the latest news articles (title link, source, time, and summary) in the right-hand column.

    Example:
        >>> news_feed()  # inside the Streamlit script
    """
    st.subheader("News")
    news = load_news()
    if isinstance(news, str) or not news:
        st.info(news or "No articles were returned.")
        return
    with st.container(height=900, border=False):
        for article in news:
            published = pd.to_datetime(article.get("publishedAt"), utc=True, errors="coerce")
            when = published.strftime("%d %b %H:%M UTC") if not pd.isna(published) else ""
            st.markdown(f"**[{article.get('title', 'No title')}]({article.get('url', '')})**")
            st.caption(f"{article.get('source', {}).get('name', 'Unknown source')} · {when}")
            if article.get("description"):
                st.write(article["description"])
            st.divider()


def main():

    st.set_page_config(page_title="BTC Trading Agent", page_icon="₿", layout="wide")
    st.title("Bitcoin Trading Agent")
    portfolio_summary()

    left, right = st.columns([3, 1], gap="large")

    with left:
        period_col, overlay_col = st.columns([1, 3])
        period = period_col.selectbox("Period", PERIODS, index=PERIODS.index("1y"))
        overlays = overlay_col.multiselect("Moving averages", list(OVERLAYS), default=["SMA_20", "EMA_20"])
        st.subheader("BTC-USD daily")
        try:
            history = load_history(period)
        except Exception: # Already logged and alerted; failures aren't cached, so reloading the page tries again
            st.error("Couldn't load the BTC-USD history from Yahoo Finance (it may be rate-limiting this server). Reload the page in a minute.")
        else:
            st.caption("Candles with volume and moving averages, then MACD (12/26/9), then RSI (14) with 70/30 guides.")
            renderLightweightCharts(build_charts(history, overlays, dark=st.context.theme.type == "dark"), key=f"btc_{period}_{'_'.join(overlays)}")

        positions_tab, trades_tab = st.tabs(["Open positions", "Trade history"])
        positions, trades, _ = load_portfolio()
        with positions_tab:
            if positions.empty:
                st.info("No open positions.")
            else:
                # Stored values are the hourly mark saved by main.py; the live columns revalue each position at the latest price
                try:
                    price, _ = load_live_price()
                except Exception: # No live price: show only the hourly marks stored by main.py
                    price = float("nan")
                positions["live_value"] = positions["btc_amnt"] * price
                positions["live_pnl"] = (price - positions["open_price"]) * positions["btc_amnt"]
                st.dataframe(positions, hide_index=True, column_order=["position_id", "btc_amnt", "open_price", "live_value", "live_pnl", "current_price", "current_value", "pnl", "date_time"], column_config={"live_value": st.column_config.NumberColumn("Live value", format="$%.2f"), "live_pnl": st.column_config.NumberColumn("Live unrealised P&L", format="$%.2f"), "position_id": "Position", "btc_amnt": st.column_config.NumberColumn("BTC", format="%.8f"), "open_price": st.column_config.NumberColumn("Avg entry", format="$%.2f"), "current_price": st.column_config.NumberColumn("Marked price", format="$%.2f"), "current_value": st.column_config.NumberColumn("Marked value", format="$%.2f"), "pnl": st.column_config.NumberColumn("Marked P&L", format="$%.2f"), "date_time": st.column_config.DatetimeColumn("Marked at (UTC)", format="D MMM YYYY, HH:mm")})
        with trades_tab:
            if trades.empty:
                st.info("No trades yet.")
            else:
                st.dataframe(trades, hide_index=True, column_config={"trade_id": "Trade", "type": "Side", "position_id": "Position", "date_time": st.column_config.DatetimeColumn("Time (UTC)", format="D MMM YYYY, HH:mm"), "btc_amnt": st.column_config.NumberColumn("BTC", format="%.8f"), "price": st.column_config.NumberColumn("Price", format="$%.2f"), "value": st.column_config.NumberColumn("Value", format="$%.2f"), "realized_pnl": st.column_config.NumberColumn("Realised P&L", format="$%.2f")})

    with right:
        news_feed()


if __name__ == "__main__":
    main()
