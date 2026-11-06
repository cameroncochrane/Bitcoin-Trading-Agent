# vlXsnPPgKKnWqEJB
Bitcoin Trading Agent

An autonomous BTC paper-trading agent. Every hour it gathers BTC-USD prices and technical indicators (Yahoo Finance),
predictions from two ML models (20-day price direction and 20-day volatility) and news headlines (NewsAPI), and an OpenAI
agent decides whether to open, add to, reduce or close positions in a simulated portfolio (starting with $10,000). Trades
are sent to Telegram, a summary is emailed every Monday (Mailtrap), and a Streamlit frontend shows the portfolio, chart
and news.

| Folder | What's in it |
|---|---|
| `btc_agent/` | The app: everything it needs to run, including the trained models in `btc_agent/models/` |
| `btc_agent/deploy/` | Running it on AWS EC2: see [btc_agent/deploy/README.md](btc_agent/deploy/README.md) |
| `experimentation/`, `modelling/`, `models/` | Notebooks and code for training the models (not used by the app) |

## Running locally

The app reads its keys from environment variables (nothing loads a `.env` file), so set them in the terminal you run from:

| Variable | Needed for |
|---|---|
| `OPENAI_API_KEY` | The agent (`main.py`) |
| `TELEGRAM_BOT_TOKEN` | `main.py`, `timer.py`, `seed_cash.py` (they won't start without it) |
| `NEWS_API_KEY` | News for the agent and the frontend |
| `MAILTRAP_API_TOKEN` | The weekly email only |
| `STARTING_CASH_BALANCE` | Optional, default 10000 |

Mac/Linux: `export OPENAI_API_KEY="sk-..."`. Windows PowerShell: `$env:OPENAI_API_KEY = "sk-..."`.

From the repo root, with Python 3.12:

```bash
python3.12 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r btc_agent/requirements.txt
```

| What | Command (from `btc_agent/`) |
|---|---|
| One agent run, now | `python main.py` |
| Run every hour (leave it running) | `python timer.py` |
| Frontend, at http://localhost:8501 | `streamlit run app.py` |

The database (`btc_agent/databases/trading.db`) and logs (`btc_agent/logs/btc_agent.log`) are created on the first run and
are never committed. A local run keeps its own portfolio, separate from the one on EC2, and costs OpenAI credits each run.
