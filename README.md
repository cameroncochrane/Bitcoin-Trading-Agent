# vlXsnPPgKKnWqEJB
Bitcoin Trading Agent

An autonomous Bitcoin **paper-trading** agent. Every hour, an LLM agent reviews BTC-USD market data, predictions from two
machine learning models and recent news, then manages a simulated portfolio on its own. No real money is traded.

## What it does

- **Every hour** (`btc_agent/main.py`, started by `btc_agent/timer.py`):
  - Fetches the live BTC-USD price and 6 months of daily candles (OHLCV)
  - Adds technical indicators: SMA and EMA (7 and 20 day), RSI (14), MACD (12/26/9), ATR (4) and intraday range
  - Marks every open position to the live price
  - Runs the **direction** and **volatility** models on the latest completed days
  - Fetches the latest Bitcoin and macro news headlines
  - Hands it all to the agent, which decides whether to open, add to, reduce or close positions, or do nothing
- **Every trade** is recorded in a SQLite database and announced on Telegram
- **Every Monday at 09:00** a portfolio summary is emailed: value, cash, open positions, P&L and the week's trades
- **A web frontend** shows the portfolio, an interactive price chart and the news at any time

## Key features

- **Autonomous LLM agent** built with the OpenAI Agents SDK. Its only tools are:
  - `get_portfolio`, `get_trade_history` and `get_cash` to look up its state (it has no memory between runs)
  - `open_position`, `update_position` and `close_position` to trade
  - `send_trade_notification` to message Telegram
- **Agent sees** 90 days of candles with indicators for its own technical analysis, both models' predictions as JSON
  (with their test metrics and known limitations), the top news headlines and its current portfolio
- **Position management:**
  - Positions are labelled `P_n` and trades `T_n`; a position can have many trades and its ID is never reused
  - Dollar cost averaging, with the average entry price recalculated on every buy
  - Partial sells and full closes, with realised P&L and cash updated automatically
- **Built-in risk limits** (enforced in code, not just in the prompt):
  - Starting cash of $10,000
  - A single buy can cost at most 10% of portfolio value, and never more than the cash available
  - Trade prices must be within 0.5% of the live price, so the agent can't trade at an invented price
  - At most 15 model calls per run
  - The agent is instructed to stop out a position 2 x ATR below its average entry price
- **Reliable records:** each trade (position, trade and cash rows) is saved in one all-or-nothing transaction
- **Resilience:**
  - Yahoo Finance requests are retried with backoff, then alerted on Telegram if they still fail
  - A failed hour skips trading and the next hour starts fresh
  - A run that hangs is stopped before the next one starts, so runs never pile up
  - Errors and every refused trade are logged (`btc_agent/logs/btc_agent.log`)
- **Frontend** (Streamlit):
  - Live portfolio value, BTC price, cash, unrealised and realised P&L, with a refresh button
  - Candlestick chart with volume and moving averages, plus MACD and RSI panes, over 3 months to all history
  - Open positions and trade history tables
  - News feed

## Models

- **Direction model** (`btc_agent/models/direction/`)
  - LSTM classifier: probability that BTC's close is higher in 20 days
  - Input: the last 48 daily closes, standard-scaled and windowed by a scikit-learn pipeline
  - Test set: ROC AUC 0.61, accuracy 54.5% (50.8% base rate)
  - A weak signal: trained on raw prices from years when BTC was much cheaper, so the agent is told to give it little weight
- **Volatility model** (`btc_agent/models/volatility/`)
  - LSTM regressor: realised volatility of daily log returns over the next 20 days
  - Input: the last 48 days of OHLCV plus return, realised-volatility (5/10/20/60 day), Parkinson and Garman-Klass
    features, robust-scaled and windowed by a scikit-learn pipeline
  - Test set: MAE 0.052, worse than the persistence baseline (last 20 days' volatility carries on) at 0.035
  - The agent gets both the model's forecast and the baseline, and is told to rely on the baseline and use volatility
    for sizing, not as a buy or sell signal
- **Training:** both models were trained on daily BTC-USD history from Yahoo Finance. Experiments (including a logistic
  regression baseline for direction) are in `experimentation/`, training code in `modelling/`, and saved models and
  evaluation plots in `models/`.

## APIs and services

- **OpenAI API:** the agent's language model
- **Yahoo Finance** (via `yfinance`): live price and daily price history
- **NewsAPI:** Bitcoin, crypto and macro-economic news headlines
- **Telegram Bot API:** trade notifications and failure alerts
- **Mailtrap:** the weekly summary email
- **AWS** (deployment):
  - EC2 runs the app
  - SSM Parameter Store holds the API keys
  - S3 stores hourly database backups (restored automatically on a new instance)
  - IAM instance role grants access, so no AWS keys are stored on the server

## Libraries

- **Agent:** `openai-agents`, `openai`
- **Machine learning:** `tensorflow` / `keras` (LSTMs), `scikit-learn` (preprocessing pipelines), `joblib`
- **Data:** `pandas`, `numpy`, `yfinance`, `requests`
- **Database:** `SQLAlchemy` with SQLite
- **Frontend:** `streamlit`, `streamlit-lightweight-charts`
- **Notifications:** `python-telegram-bot`, `mailtrap`
- **Deployment:** systemd units and Bash scripts, the AWS CLI

## Repository layout

- `btc_agent/`: the app, with everything it needs to run
  - `main.py`: one hourly cycle
  - `timer.py`: scheduler that runs `main.py` every hour and sends the weekly email
  - `app.py`: Streamlit frontend
  - `seed_cash.py`: set or top up the cash balance by hand
  - `src/`: agent, price data, indicators, volatility features, predictions, news, notifications, weekly report,
    database and logging
  - `models/`: the trained models the app uses
  - `deploy/`: AWS EC2 deployment (systemd units, secrets and backup scripts, IAM policy); see
    [btc_agent/deploy/README.md](btc_agent/deploy/README.md)
- `experimentation/`: notebooks from developing the direction and volatility models
- `modelling/`: model training and evaluation code
- `models/`: saved models and evaluation results from training
- `data/`: training data (not committed)

## Running locally

- **Requirements:** Python 3.12 and API keys for OpenAI, NewsAPI, Telegram and (for the weekly email only) Mailtrap
- **Install,** from the repo root:
  ```bash
  python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
  pip install -r btc_agent/requirements.txt
  ```
- **Set the keys** as environment variables in the terminal you run from (nothing loads a `.env` file):
  - `OPENAI_API_KEY`, `NEWS_API_KEY`, `TELEGRAM_BOT_TOKEN`, `MAILTRAP_API_TOKEN`
  - Optional: `STARTING_CASH_BALANCE` (default 10000)
  - Mac/Linux: `export OPENAI_API_KEY="sk-..."`; Windows PowerShell: `$env:OPENAI_API_KEY = "sk-..."`
- **Run,** from `btc_agent/`:
  - `python main.py`: one agent run now
  - `python timer.py`: run every hour (leave it running)
  - `streamlit run app.py`: the frontend, at http://localhost:8501
- **Notes:**
  - The database (`btc_agent/databases/trading.db`) and logs are created on the first run and never committed
  - A local run keeps its own portfolio, separate from the deployed one, and each run uses OpenAI credits

## Deployment

- Runs on an AWS EC2 instance under systemd: the hourly agent, the frontend and hourly S3 backups start on boot and
  restart on failure
- Step-by-step setup, day-to-day commands and troubleshooting: [btc_agent/deploy/README.md](btc_agent/deploy/README.md)

## Disclaimer

- This is a paper-trading research project. It trades a simulated portfolio only and is not financial advice.

## License

- MIT: see [LICENSE](LICENSE)
