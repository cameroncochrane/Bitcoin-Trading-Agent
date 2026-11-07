# This script contains the LLM and agentic functionality.

# What functionality does the agent have to be able to trade?:
    # - Inspect the current portfolio and trade history (it may have run before and not remember, so it re-orients itself through these tools every run)
    # - Open/update/close positions in trading.db (each one automatically records the trade, its realised pnl, and the cash change)
    # - Send a Telegram notification when a trade is executed

import json
import logging
import os
from datetime import datetime, timezone

import pandas as pd
import sqlalchemy
from agents import Agent, FunctionTool, MaxTurnsExceeded, Runner, function_tool, set_default_openai_key

from src.database.database import (
    CASH_ROW_ID,
    add_row,
    fetch_table_data,
    get_engine,
    modify_row,
    portfolio_table,
    remove_row,
    trades_table,
    cash_table
)
from src.notification import chat_id, send_telegram_message

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = "gpt-6-luna"
PRICE_HISTORY_DAYS = 90 # How many of the most recent daily candles (OHLCV + indicators) are shown to the agent for its own technical analysis
STARTING_CASH_BALANCE = float(os.getenv("STARTING_CASH_BALANCE", "10000")) # $10,000. Only used the very first time the cash table is seeded
PRICE_TOLERANCE = 0.005 # Trades are refused if the price the agent passes differs from the live price by more than this fraction (0.5%)
MAX_TRADE_FRACTION = 0.10 # A single buy can cost at most this fraction (10%) of total portfolio value (cash + open BTC at the trade price)
TRADE_HISTORY_LIMIT = 100 # get_trade_history shows the agent only this many of the most recent trades, so the prompt doesn't grow every hour
MAX_TURNS = 15 # Most model calls one hourly run may make; a normal cycle needs about 4-6 (see run_agent)
STOP_LOSS_ATR_MULTIPLE = 2.0 # The agent is told to close a position once the price falls this many ATR_4s below its average entry price

logger = logging.getLogger(__name__)
_live_price: float | None = None # The latest BTC price supplied by main.py for this cycle; set by run_agent and checked by the trade tools


def _log_tool_error(ctx: object, error: Exception) -> str:
    """Log a tool failure with its full traceback, and tell the agent the tool failed without exposing internal details.

    Used as every tool's failure_error_function. Without it the Agents SDK swallows the exception, so failures never reach the logs.
    The trade tools run in a single transaction, so a failure part-way through leaves the database unchanged.

    Example:
        >>> @function_tool(failure_error_function=_log_tool_error)
        ... def my_tool() -> str: ...
    """
    tool_name = getattr(ctx, "tool_name", "unknown tool")
    logger.error("Tool %s failed: %s", tool_name, error, exc_info=error)
    return f"The {tool_name} tool failed ({type(error).__name__}) and nothing was changed. The error has been logged; don't retry the same call this cycle."


def _check_price(price: float) -> str | None:
    """Return why ``price`` can't be used for a trade (it doesn't match this cycle's live price), or None if it can.

    The agent passes the execution price to each trade tool; this makes sure it used the live price main.py supplied rather than
    a stale, rounded, or invented one. Skipped when no live price has been set (e.g. when a tool is called outside run_agent).

    Example:
        >>> _check_price(82000.0)  # None if the live price is within 0.5% of 82000
    """
    if _live_price is None:
        return None
    if price <= 0 or abs(price - _live_price) / _live_price > PRICE_TOLERANCE:
        logger.warning("Trade refused: agent passed price %.2f but the live price is %.2f.", price, _live_price)
        return f"price {price:.2f} doesn't match the live BTC price {_live_price:.2f}. Use the latest price supplied to you"
    return None


@function_tool(failure_error_function=_log_tool_error)
def get_portfolio() -> list[dict]:
    """Return every open position currently held, as rows from the portfolio table."""
    engine = get_engine()
    return fetch_table_data(engine, portfolio_table)


@function_tool(failure_error_function=_log_tool_error)
def get_trade_history() -> dict:
    """Return the most recent trades (at most 100, oldest first), plus totals from the full history.

    Only the last TRADE_HISTORY_LIMIT trades are returned so the prompt doesn't grow every hour. Because older position ids
    drop out of that window, it also returns next_position_id (the next unused P_n) and the all-time trade count and realised pnl.

    Example:
        >>> get_trade_history()
        {'recent_trades': [...], 'total_trades': 120, 'total_realized_pnl': 431.5, 'next_position_id': 'P_38'}
    """
    engine = get_engine()
    trades = fetch_table_data(engine, trades_table)
    positions = fetch_table_data(engine, portfolio_table)
    trades.sort(key=lambda row: int(row["trade_id"][2:]) if row["trade_id"][2:].isdigit() else 0) # T_n order is execution order
    used = [int(pid[2:]) for pid in {row["position_id"] for row in trades + positions} if pid.startswith("P_") and pid[2:].isdigit()]
    return {"recent_trades": trades[-TRADE_HISTORY_LIMIT:], "total_trades": len(trades), "total_realized_pnl": sum(row["realized_pnl"] for row in trades), "next_position_id": f"P_{max(used, default=0) + 1}"}


@function_tool(failure_error_function=_log_tool_error)
def get_cash() -> dict | str:
    """Return the current cash balance and when it was last updated, from the cash table."""
    engine = get_engine()
    rows = fetch_table_data(engine, cash_table)
    if not rows:
        return "No cash balance has been recorded yet."
    return {"cash": rows[0]["cash"], "date_time": rows[0]["date_time"]}


def _cash_balance(bind: sqlalchemy.Engine | sqlalchemy.Connection) -> float | None:
    """Return the current cash balance, or None if no balance has been recorded yet.

    Example:
        >>> _cash_balance(get_engine())
        10000.0
    """
    rows = fetch_table_data(bind, cash_table)
    return rows[0]["cash"] if rows else None


def _set_cash(bind: sqlalchemy.Engine | sqlalchemy.Connection, cash: float) -> None:
    """Set the single cash row to ``cash``, creating the row on the first ever update.

    Example:
        >>> _set_cash(get_engine(), 10000.0)  # e.g. to seed the starting balance
    """
    row = {"cash_id": CASH_ROW_ID, "cash": cash, "date_time": datetime.now(timezone.utc)}
    try:
        modify_row(bind, cash_table, row)
    except LookupError: # No cash row exists yet (first ever update), so create it
        add_row(bind, cash_table, row)


def seed_cash_if_empty(bind: sqlalchemy.Engine | sqlalchemy.Connection, starting_cash: float = STARTING_CASH_BALANCE) -> None:
    """Seed the cash table with ``starting_cash`` only if no balance has ever been recorded.

    A no-op once any balance exists (seeded, traded, or manually adjusted), so calling this on every
    run_agent invocation is safe: a restart or redeploy never resets cash back to the starting balance.

    Example:
        >>> seed_cash_if_empty(get_engine())  # seeds on the very first call, a no-op afterwards
    """
    if _cash_balance(bind) is None:
        _set_cash(bind, starting_cash)


def _record_trade(bind: sqlalchemy.Engine | sqlalchemy.Connection, trade_type: str, position_id: str, btc_amnt: float, price: float, realized_pnl: float) -> str:
    """Record a 'buy' or 'sell' against ``position_id`` under the next unused T_n id, apply it to cash, and return the trade id.

    A buy takes btc_amnt * price out of cash and a sell adds it back. Called by the open/update/close position tools,
    so every trade is recorded and settled the same way; the agent never records trades or edits cash itself.

    Example:
        >>> _record_trade(engine, "buy", "P_1", 0.1, 60000, 0.0)
        'T_1'
    """
    trade_numbers = [int(row["trade_id"][2:]) for row in fetch_table_data(bind, trades_table) if row["trade_id"].startswith("T_") and row["trade_id"][2:].isdigit()]
    trade_id = f"T_{max(trade_numbers, default=0) + 1}"
    value = btc_amnt * price
    add_row(bind, trades_table, {"trade_id": trade_id, "type": trade_type, "position_id": position_id, "date_time": datetime.now(timezone.utc), "btc_amnt": btc_amnt, "price": price, "value": value, "realized_pnl": realized_pnl})
    _set_cash(bind, (_cash_balance(bind) or 0.0) + (value if trade_type == "sell" else -value))
    return trade_id


def _check_buy(bind: sqlalchemy.Engine | sqlalchemy.Connection, btc_amnt: float, price: float) -> str | None:
    """Return why a buy of ``btc_amnt`` at ``price`` can't go ahead, or None if it can.

    A buy is refused if no cash balance has been recorded, if it costs more than the cash available, or if it costs more than
    MAX_TRADE_FRACTION (10%) of total portfolio value: cash plus every open position valued at ``price``. Only buys are limited,
    so the agent can always sell or close a position, however large it has become.

    Example:
        >>> _check_buy(engine, 0.01, 80000)  # None if 800 is affordable and at most 10% of portfolio value
    """
    cash = _cash_balance(bind)
    if cash is None:
        logger.warning("Trade refused: buy attempted but no cash balance has been recorded.")
        return "no cash balance has been recorded yet"
    cost = btc_amnt * price
    if cost > cash + 1e-9:
        logger.warning("Trade refused: buy of %.2f is more than the %.2f cash available.", cost, cash)
        return f"buying {btc_amnt} BTC at {price:.2f} costs {cost:.2f} but only {cash:.2f} cash is available"
    portfolio_value = cash + sum(row["btc_amnt"] * price for row in fetch_table_data(bind, portfolio_table))
    max_cost = MAX_TRADE_FRACTION * portfolio_value
    if cost > max_cost + 1e-9:
        logger.warning("Trade refused: buy of %.2f is over the %.0f%% limit (%.2f of %.2f portfolio value).", cost, MAX_TRADE_FRACTION * 100, max_cost, portfolio_value)
        return f"buying {btc_amnt} BTC at {price:.2f} costs {cost:.2f}, over the {MAX_TRADE_FRACTION:.0%} limit of {max_cost:.2f} (portfolio value {portfolio_value:.2f}). The most you can buy in one trade is {max_cost / price:.8f} BTC"
    return None


@function_tool(failure_error_function=_log_tool_error)
def open_position(position_id: str, btc_amnt: float, price: float) -> str:
    """Open a new BTC position by buying ``btc_amnt`` BTC at ``price``.

    Inserts the position into the portfolio table, records the buy as a trade (next T_n id) and takes its cost out of cash.

    Args:
        position_id: Unique position label of the form "P_n" (e.g. "P_1"). Never reuse an id that appears in the trade history.
        btc_amnt: BTC to buy (positive, e.g. 0.1).
        price: The BTC price the trade executes at. Must be the latest price supplied to you; a trade more than 0.5% away from it is refused.

    Example:
        >>> open_position("P_1", 0.1, 60000)  # costs 6000 cash
    """
    with get_engine().begin() as connection: # One transaction: the position, trade, and cash rows are all written, or none are
        # A position id is used once for the life of the portfolio. Closed positions are removed from the portfolio table, so check the trade history too
        used_ids = {row["position_id"] for row in fetch_table_data(connection, portfolio_table)}
        used_ids |= {row["position_id"] for row in fetch_table_data(connection, trades_table)}
        if position_id in used_ids:
            logger.warning("Trade refused: open_position used %s, which has already been used.", position_id)
            return f"Position not opened: {position_id} has already been used. Choose a new, unused P_n id."
        if reason := _check_price(price):
            return f"Position not opened: {reason}."
        btc_amnt = round(btc_amnt, 8) # Round to 1 satoshi (the smallest BTC unit)
        if btc_amnt <= 0:
            logger.warning("Trade refused: open_position for %s had a non-positive btc_amnt (%s).", position_id, btc_amnt)
            return "Position not opened: btc_amnt must be positive."
        if reason := _check_buy(connection, btc_amnt, price):
            return f"Position not opened: {reason}."

        add_row(connection, portfolio_table, {"position_id": position_id, "btc_amnt": btc_amnt, "open_price": price, "current_price": price, "current_value": btc_amnt * price, "pnl": 0.0, "date_time": datetime.now(timezone.utc)})
        trade_id = _record_trade(connection, "buy", position_id, btc_amnt, price, 0.0)
        logger.info("Opened %s: bought %s BTC at %.2f (%s).", position_id, btc_amnt, price, trade_id)
        return f"Position {position_id} opened: bought {btc_amnt} BTC at {price:.2f} (trade {trade_id}). Cash is now {_cash_balance(connection):.2f}."


@function_tool(failure_error_function=_log_tool_error)
def update_position(position_id: str, btc_change: float, price: float) -> str:
    """Add to (buy) or reduce (sell part of) an open position, recalculating its average entry price.

    Buying more BTC (dollar cost averaging) sets open_price to the size-weighted average entry price:
    (old_btc * old_open_price + btc_change * price) / new_btc. Selling part of a position keeps open_price
    unchanged and realises pnl on the sold BTC: (price - open_price) * btc_sold.
    current_price, current_value and pnl (unrealised) are then marked to the trade price.
    The trade is recorded (next T_n id) and cash is updated automatically. To sell the whole position use close_position instead.

    Args:
        position_id: The "P_n" label of an open position.
        btc_change: BTC bought (positive, e.g. 0.05) or sold (negative, e.g. -0.02) in this trade.
        price: The BTC price the trade executes at. Must be the latest price supplied to you; a trade more than 0.5% away from it is refused.

    Example:
        >>> update_position("P_1", 0.1, 60000)  # P_1 held 0.1 BTC at 50000 -> 0.2 BTC at an average of 55000
    """
    with get_engine().begin() as connection: # One transaction: the position, trade, and cash rows are all written, or none are
        position = next((row for row in fetch_table_data(connection, portfolio_table) if row["position_id"] == position_id), None)
        if position is None:
            logger.warning("Trade refused: update_position used %s, which is not an open position.", position_id)
            return f"Position not updated: {position_id} is not an open position."
        if reason := _check_price(price):
            return f"Position not updated: {reason}."
        btc_change = round(btc_change, 8) # Round to 1 satoshi (the smallest BTC unit)
        if btc_change == 0:
            logger.warning("Trade refused: update_position for %s had a zero btc_change.", position_id)
            return "Position not updated: btc_change must be non-zero."

        old_btc, open_price = position["btc_amnt"], position["open_price"]
        new_btc = round(old_btc + btc_change, 8) # Round to 1 satoshi to avoid float drift such as 0.30000000000000004
        if new_btc <= 0:
            logger.warning("Trade refused: selling %s BTC would empty or oversell %s, which holds %s BTC.", -btc_change, position_id, old_btc)
            return f"Position not updated: {position_id} holds {old_btc} BTC, so selling {-btc_change} BTC would empty or oversell it. Use close_position to sell the whole position."
        if btc_change > 0 and (reason := _check_buy(connection, btc_change, price)):
            return f"Position not updated: {reason}."

        realized_pnl = 0.0
        if btc_change > 0:
            open_price = (old_btc * open_price + btc_change * price) / new_btc
        else:
            realized_pnl = (price - open_price) * -btc_change

        modify_row(connection, portfolio_table, {"position_id": position_id, "btc_amnt": new_btc, "open_price": open_price, "current_price": price, "current_value": new_btc * price, "pnl": (price - open_price) * new_btc, "date_time": datetime.now(timezone.utc)})
        trade_id = _record_trade(connection, "buy" if btc_change > 0 else "sell", position_id, abs(btc_change), price, realized_pnl)
        logger.info("Updated %s: %+g BTC at %.2f, realised pnl %.2f (%s).", position_id, btc_change, price, realized_pnl, trade_id)
        return f"Position {position_id} updated (trade {trade_id}): now {new_btc} BTC at an average entry price of {open_price:.2f}. Realised pnl: {realized_pnl:.2f}. Cash is now {_cash_balance(connection):.2f}."


@function_tool(failure_error_function=_log_tool_error)
def close_position(position_id: str, price: float) -> str:
    """Close a position entirely by selling all of its BTC at ``price``.

    Realises pnl on the whole position, (price - open_price) * btc_amnt, records the sell as a trade (next T_n id),
    adds the proceeds to cash, and removes the position from the portfolio table.

    Args:
        position_id: The "P_n" label of an open position.
        price: The BTC price the trade executes at. Must be the latest price supplied to you; a trade more than 0.5% away from it is refused.

    Example:
        >>> close_position("P_1", 70000)  # P_1 held 0.2 BTC at an average of 55000 -> realised pnl 3000, cash +14000
    """
    with get_engine().begin() as connection: # One transaction: the position, trade, and cash rows are all written, or none are
        position = next((row for row in fetch_table_data(connection, portfolio_table) if row["position_id"] == position_id), None)
        if position is None:
            logger.warning("Trade refused: close_position used %s, which is not an open position.", position_id)
            return f"Position not closed: {position_id} is not an open position."
        if reason := _check_price(price):
            return f"Position not closed: {reason}."

        btc_amnt = position["btc_amnt"]
        realized_pnl = (price - position["open_price"]) * btc_amnt
        remove_row(connection, portfolio_table, {"position_id": position_id})
        trade_id = _record_trade(connection, "sell", position_id, btc_amnt, price, realized_pnl)
        logger.info("Closed %s: sold %s BTC at %.2f, realised pnl %.2f (%s).", position_id, btc_amnt, price, realized_pnl, trade_id)
        return f"Position {position_id} closed (trade {trade_id}): sold {btc_amnt} BTC at {price:.2f}. Realised pnl: {realized_pnl:.2f}. Cash is now {_cash_balance(connection):.2f}."


@function_tool(failure_error_function=_log_tool_error)
def send_trade_notification(message: str) -> str:
    """Send a Telegram message, e.g. to report a trade that was just executed."""
    send_telegram_message(chat_id, message)
    return "Notification sent."


AGENT_INSTRUCTIONS = f"""
You are an autonomous BTC portfolio-management agent. You are invoked roughly once an hour and
have no memory of previous runs other than what is stored in the database, so always start by
calling get_portfolio, get_trade_history, and get_cash to see what positions you are already
holding, what you have done before, and how much cash you have available.

Given the latest price, recent price history, model predictions, and news context supplied in
the prompt, decide whether to open a new position, adjust or close an existing one, or do
nothing this cycle. Whenever a trade actually executes, call send_trade_notification with a short
human-readable summary of what you did and why.

Finish by returning a brief summary of your decision and reasoning.

Some additional caveats:
- When opening a position use the next_position_id returned by get_trade_history (the first ever position is P_1). Ids are
never reused, even after a position is closed. get_trade_history only lists the last {TRADE_HISTORY_LIMIT} trades, so rely on next_position_id
rather than working the next id out yourself. A position can have several trades (T_n) against it, e.g. its opening buy,
later buys or partial sells, and its closing sell.

- Dollar cost averaging is allowed: to add to or reduce an open position, call update_position with the BTC bought (positive)
or sold (negative). It recalculates the position's average entry price. To sell a whole position, use close_position.

- Buys are refused if there isn't enough cash, or if a single buy would cost more than {MAX_TRADE_FRACTION:.0%} of total portfolio value (cash plus
open positions at the current price). Size every buy within both limits; check get_cash and get_portfolio first. Sells and
closes are never limited.

- Don't hallucinate the current price. Pass the latest BTC price provided explicitly to you as the price for every trade;
trades at a price more than {PRICE_TOLERANCE:.1%} away from it are refused.

- You're given up to {PRICE_HISTORY_DAYS} days of daily BTC-USD candles (open, high, low, close, volume) with pre-calculated
indicators (SMA, EMA, RSI, MACD, ATR). Use them for your own technical analysis, e.g. trend, support and resistance,
momentum, and volatility. Prefer the pre-calculated indicator values over recalculating them yourself, since mental
arithmetic over many rows is error-prone. The last candle is today's and is still forming.

- Stop-loss: use the latest ATR_4 (4-day average true range, in dollars, from the most recent row of the candles) to decide
when to cut a losing position. If the live price has fallen more than {STOP_LOSS_ATR_MULTIPLE:g} x ATR_4 below a position's
average entry price (open_price), close it with close_position, even if other signals look positive. For example, with an
entry of 100,000 and ATR_4 of 2,500, close once the price is below {100000 - STOP_LOSS_ATR_MULTIPLE * 2500:,.0f}.

- Model predictions are given as JSON, one entry per model, each with a "status". Only use a model whose status is "ok";
ignore one that is "unavailable" or "error". Each entry includes the model's test metrics and known limitations: weigh it
accordingly, and never trade on a model prediction alone.

- The direction model ("direction") is a weak signal. On its test set it scored a ROC AUC of 0.61 (0.5 is a coin flip)
and 54.5% accuracy against a 50.8% base rate, and it predicted "down" about 85% of the time, catching only about 20% of
actual rises. It was trained on raw prices from years when BTC was far cheaper, so at today's prices its output barely
changes (around 0.25) whatever the market does. Give it little or no weight, especially when its entry has a "warning".

- The volatility model ("volatility") forecasts how much BTC is likely to move over the next 20 days (realised volatility of
daily log returns, e.g. 0.06 means a typical 20-day move of about 6%), not which way. On its test set it was worse than the
persistence baseline (assuming the last 20 days' realised volatility carries on), so use "persistence_baseline_20d" as
your main volatility estimate and the model's "predicted_volatility_20d" only as a weak second opinion. Use volatility to
size and pace trades, e.g. smaller buys when expected volatility is high, never as a buy or sell signal by itself.

- Portfolio value can be calculated by considering position values and the cash present (returned by get_cash).

- After trades have been executed, send a Telegram message via the send_trade_notification tool you have.

"""


AGENT_TOOLS = [get_portfolio, get_trade_history, get_cash, open_position, update_position, close_position, send_trade_notification]


def _format_tool_list(tools: list[FunctionTool]) -> str:
    """Render the agent's tools as a list for its instructions, using each tool's name and the first line of its docstring.

    Built from the same list passed to Agent(tools=...), so the instructions can't drift from the tools the agent really has.

    Example:
        >>> print(_format_tool_list([get_cash]))
        Tools available to you (these are the only tools you have):
        - get_cash: Return the current cash balance and when it was last updated, from the cash table.
    """
    lines = [f"- {tool.name}: {tool.description.splitlines()[0]}" for tool in tools]
    return "\nTools available to you (these are the only tools you have):\n" + "\n".join(lines) + "\n"


def _format_context(context: dict | None) -> str:
    """Render the price/news/prediction context gathered in main.py into a prompt the agent can read."""
    context = context or {}
    sections = []

    current_price = context.get("current_price")
    if current_price is not None and not current_price.empty:
        sections.append(f"Latest BTC-USD quote:\n{current_price.iloc[0].to_dict()}")

    historical_price = context.get("historical_price")
    if historical_price is not None and not historical_price.empty:
        start_close = historical_price["Close"].iloc[0]
        end_close = historical_price["Close"].iloc[-1]
        pct_change = (end_close - start_close) / start_close * 100
        sections.append(
            "Historical price summary:\n"
            f"start_close={start_close:.2f}, end_close={end_close:.2f}, "
            f"min_close={historical_price['Close'].min():.2f}, "
            f"max_close={historical_price['Close'].max():.2f}, "
            f"pct_change={pct_change:.2f}%"
        )

        # Full daily candles (OHLCV) so the agent can run its own technical analysis, plus the indicators added in main.py.
        # CSV keeps it compact; indicators are blank during their warm-up period (e.g. the first 19 days of SMA_20)
        indicator_columns = [column for column in historical_price.columns if column.startswith(("SMA_", "EMA_", "RSI_", "MACD", "ATR_", "IDR"))]
        price_columns = [column for column in ("Date", "Open", "High", "Low", "Close", "Volume") if column in historical_price.columns]
        recent = historical_price[price_columns + indicator_columns].tail(PRICE_HISTORY_DAYS).copy()
        if "Date" in recent.columns:
            recent["Date"] = pd.to_datetime(recent["Date"]).dt.strftime("%Y-%m-%d")
        if "Volume" in recent.columns:
            recent["Volume"] = recent["Volume"].round().astype("Int64")
        sections.append(
            f"Daily BTC-USD candles and technical indicators (last {len(recent)} days, oldest first, CSV). "
            "The last row is today's candle, still forming, so its close is roughly the live price:\n"
            f"{recent.to_csv(index=False, float_format='%.2f').strip()}"
        )

    predictions = context.get("predictions")
    if predictions:
        sections.append(f"Model predictions (JSON):\n{json.dumps(predictions, indent=2, default=str)}")

    news = context.get("news")
    if news:
        headlines = "\n".join(f"- {article.get('title', 'No title')}" for article in news[:5])
        sections.append(f"Recent news headlines:\n{headlines}")

    current_portfolio = context.get("current_portfolio")
    if current_portfolio:
        sections.append(f"Portfolio snapshot passed in from main.py:\n{current_portfolio}")

    return "\n\n".join(sections) if sections else "No market context was supplied this run."


def run_agent(api_token: str | None = None, context: dict | None = None) -> str:
    """Run the trading agent for one cycle and return its final summary.

    The agent re-orients itself every call via the get_portfolio/get_trade_history tools, then
    uses the price/news/prediction context passed in `context` to decide whether to trade.

    Example:
        >>> run_agent(context={"current_price": current_price, "news": news})
    """
    token = api_token or OPENAI_API_KEY
    if not token:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set.")
    set_default_openai_key(token)

    seed_cash_if_empty(get_engine()) # No-op after the first ever call; never resets an existing balance

    global _live_price
    current_price = (context or {}).get("current_price")
    _live_price = float(current_price["Close"].iloc[0]) if current_price is not None and not current_price.empty else None
    if _live_price is None:
        logger.warning("No live price was supplied, so trade prices won't be checked this cycle.")

    agent = Agent(
        name="BTC Portfolio Agent",
        instructions=AGENT_INSTRUCTIONS + _format_tool_list(AGENT_TOOLS),
        model=MODEL,
        tools=AGENT_TOOLS,
    )

    logger.info("Running the agent (live price %s).", f"{_live_price:.2f}" if _live_price else "unknown")
    try:
        # max_turns caps the model calls in one run, so a confused or looping agent can't run up cost or overlap the next hourly run
        result = Runner.run_sync(agent, _format_context(context), max_turns=MAX_TURNS)
    except MaxTurnsExceeded:
        # Each trade tool commits on its own, so any trades made before the limit was hit are saved and consistent
        logger.error("Agent stopped after reaching the %d-turn limit; any trades made before that are saved.", MAX_TURNS)
        return f"Agent stopped after reaching the {MAX_TURNS}-turn limit."
    logger.info("Agent finished: %s", result.final_output)
    return result.final_output
