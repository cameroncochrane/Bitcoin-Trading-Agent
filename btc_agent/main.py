# Backend which orchestrates the overall agent. It is run every hour, being invoked according to timer logic in 'timer.py' (this directory)

import logging
from datetime import datetime, timezone

import pandas as pd

# Paths:
from src.paths import DIRECTION_MODEL_DIR, VOLATILITY_MODEL_DIR

# Custom modules:
from src.price_data import fetch_live_BTCUSD, fetch_historical_BTCUSD
from src.price_indicators import add_SMA, add_EMA, add_ranges, add_RSI, add_MACD
from src.volatility_features import add_volatility_features
from src.news import fetch_news_articles
from src.predictions import predict_direction, predict_volatility, RollingWindowTransformer # RollingWindowTransformer must be imported here (at module level) for the direction and volatility pipelines to load
from src.agent import run_agent
from src.database.database import create_database, modify_row, fetch_table_data, portfolio_table, trades_table
from src.notification import send_email
from src.logger import setup_logging

logger = logging.getLogger(__name__)


def main():

    def return_price():
        current_price = fetch_live_BTCUSD() # Fetch latest price
        # Six months of daily history: the volatility model needs about 109 completed days (60-day warm-up for rv_60d plus its
        # 48-day lookback). The agent is shown only the last 90 days of candles (PRICE_HISTORY_DAYS in src/agent.py)
        price_history = fetch_historical_BTCUSD(period="6mo")

        return current_price, price_history

    current_price, price_history = return_price()

    def add_price_indicators(historical_data):
        """
        Add technical indicators to the daily historical data so the agent can see them (it can't calculate them itself).
        """
        # These indicators are for the LLM to assess. None of them are actually needed for model predictions:
        data = add_SMA(data=historical_data, n=7)
        data = add_SMA(data=data, n=20)
        data = add_EMA(data=data, n=7)
        data = add_EMA(data=data, n=20)
        data = add_ranges(data=data)
        data = add_RSI(data=data)
        data = add_MACD(data=data)

        return data

    price_history = add_price_indicators(price_history)

    def update_portfolio(current_price: pd.DataFrame) -> list[dict[str, object]]:
        """
        Mark every open position to the latest BTC price, so the portfolio table (and the agent) see up-to-date values.

        For each row in the portfolio table this sets current_price to the latest close, current_value to
        btc_amnt * current_price, and pnl (unrealised) to (current_price - open_price) * btc_amnt.
        Returns the updated open positions as dictionaries (an empty list if no positions are open).

        Example:
            >>> current_price = fetch_live_BTCUSD()
            >>> current_portfolio = update_portfolio(current_price)
            >>> current_portfolio[0]["pnl"]
            250.0
        """
        price = float(current_price["Close"].iloc[0])
        updated_at = datetime.now(timezone.utc)
        engine = create_database()

        for position in fetch_table_data(engine, portfolio_table):
            btc_amnt = position["btc_amnt"]
            modify_row(engine, portfolio_table, {"position_id": position["position_id"], "current_price": price, "current_value": btc_amnt * price, "pnl": (price - position["open_price"]) * btc_amnt, "date_time": updated_at})

        return fetch_table_data(engine, portfolio_table)

    current_portfolio = update_portfolio(current_price)
    news = fetch_news_articles() # Fetch the latest news articles for the LLM to use

    def models_predict(historical_data, current_price):
        """
        Make predictions using the trained ML models and output them for the LLM to use. Expects current and historical price data (length of choosing).
        We are making an inference here so it isn't expecting Direction_x (y) data, only X.
        """

        DIRECTION_PREPROCESSOR_NAME = "process_pipeline.joblib"
        DIRECTION_MODEL_NAME = "model_simple.keras" 

        VOLATILITY_MODEL_NAME = "vol_model_simple.keras"
        VOLATILITY_PREPROCESSOR_NAME = "vol_pipeline.joblib"
        VOLATILITY_EVALUATION_NAME = "vol_model_evaluation.json" # Test metrics saved by the volatility notebook

        d_model_path = DIRECTION_MODEL_DIR / DIRECTION_MODEL_NAME
        d_preprocessor_path = DIRECTION_MODEL_DIR / DIRECTION_PREPROCESSOR_NAME
        v_model_path = VOLATILITY_MODEL_DIR / VOLATILITY_MODEL_NAME
        v_preprocessor_path = VOLATILITY_MODEL_DIR / VOLATILITY_PREPROCESSOR_NAME
        v_evaluation_path = VOLATILITY_MODEL_DIR / VOLATILITY_EVALUATION_NAME

        # Today's daily candle is still forming (its close is effectively the live price), but both models were trained on
        # completed days only, so it's dropped here rather than appending the current price
        completed = historical_data[pd.to_datetime(historical_data["Date"]).dt.date < datetime.now(timezone.utc).date()]
        as_of_date = str(pd.to_datetime(completed["Date"].iloc[-1]).date())

        direction_data = completed[["Close"]] # Direction pipeline only needs the "Close" column

        # Volatility pipeline needs OHLCV plus the volatility features it was trained on. calculate_y=False: no future label at
        # inference, so dropna only removes the warm-up rows and the latest completed day is kept
        volatility_data = add_volatility_features(completed[["Open", "High", "Low", "Close", "Volume"]], calculate_y=False)

        # Each model returns its prediction, model name/type, and test metrics so the agent can assess reliability.
        # A failing model is logged and reported as an error, so it never stops the agent from running.
        predictions = {}
        try:
            predictions["direction"] = predict_direction(direction_data, d_model_path, d_preprocessor_path, as_of_date)
        except Exception as error:
            logger.exception("Direction model prediction failed.")
            predictions["direction"] = {"status": "error", "reason": f"{type(error).__name__}: {error}"}

        if not (VOLATILITY_MODEL_NAME and VOLATILITY_PREPROCESSOR_NAME):
            predictions["volatility"] = {"status": "unavailable", "reason": "No volatility model is configured."}
        else:
            try:
                predictions["volatility"] = predict_volatility(volatility_data, v_model_path, v_preprocessor_path, as_of_date, v_evaluation_path)
            except Exception as error:
                logger.exception("Volatility model prediction failed.")
                predictions["volatility"] = {"status": "error", "reason": f"{type(error).__name__}: {error}"}

        logger.info("Model predictions: %s", {name: result.get("probability_up", result.get("predicted_volatility_20d", result.get("status"))) for name, result in predictions.items()})
        return predictions

    predictions = models_predict(price_history, current_price)

    def ask_agent_portfolio(current_price, historical_price, current_portfolio, predictions, news):
        """
        Run the agent by providing all the information it needs to make decisions regarding portfolio management.
        Trade history isn't passed here: the agent fetches its last 100 trades itself with the get_trade_history tool.
        """
        context = {
            "current_price": current_price,
            "historical_price": historical_price,
            "current_portfolio": current_portfolio,
            "predictions": predictions,
            "news": news,
        }
        return run_agent(api_token=None, context=context) # Imported from src.agent

    ask_agent_portfolio(current_price, price_history, current_portfolio, predictions, news) # run_agent logs the agent's summary

    
if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        logger.exception("Hourly run failed.") # Full traceback goes to logs/btc_agent.log and stderr (journalctl under systemd)
        raise SystemExit(1)
