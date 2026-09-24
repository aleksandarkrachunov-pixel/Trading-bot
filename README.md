# Trading Bot

A fully automated crypto trading bot in Python. It **backtests**, **paper trades** and
**live trades** on any [ccxt](https://github.com/ccxt/ccxt)-supported exchange (Binance,
Kraken, Coinbase, Bybit, OKX, …) and uses the same strategy, risk and order logic
in all three modes, so what you backtest is what runs live.

> ⚠️ **Risk warning.** Trading is risky and most automated strategies lose money after fees.
> The included strategies are examples, not a promise of profit. Backtest, then paper trade
> for weeks, then go live with an amount you can afford to lose. You are responsible for
> any trades this software places.

## Features

- **Strategies:** SMA/EMA trend-following crossover, RSI mean reversion with a trend filter. Adding your own takes about 20 lines.
- **Risk management:**
  - volatility-based (ATR) position sizing that risks a fixed % of equity per trade
  - ATR stop-loss, optional trailing stop and take-profit
  - **kill switch:** flattens and halts at a max drawdown from peak equity
  - daily loss limit
  - position cap, no leverage, spot only, long only
- **Backtester:** bar by bar with no look-ahead, fees, slippage and gap-aware stop fills. Reports Sharpe, Sortino, CAGR, max drawdown, win rate, profit factor, exposure and a buy-and-hold comparison.
- **Optimizer:** grid search ranked on a training slice and checked on held-out data, so you can spot overfitting.
- **Live engine:**
  - acts only on closed candles
  - checks stops on every poll
  - retries network errors with backoff
  - saves state to disk after each step, so it resumes after a restart or crash
  - graceful shutdown on Ctrl-C / SIGTERM, and a `STOP` file kill switch
- **Safety defaults:** paper mode by default. Live trading needs API keys *and* the `--confirm-live` flag.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml      # edit exchange, symbol, strategy, risk
```

### 1. Backtest

```bash
# offline, synthetic data
python -m tradingbot backtest --synthetic 3000

# real history (downloads from the exchange in config.yaml)
python -m tradingbot backtest --since 2023-01-01 --trades-out trades.csv --equity-out equity.csv

# or save the data once and reuse it
python -m tradingbot download --since 2022-01-01 --out data/btc_1h.csv
python -m tradingbot backtest --csv data/btc_1h.csv
```

### 2. Tune parameters (with out-of-sample check)

```bash
python -m tradingbot optimize --csv data/btc_1h.csv --grid "fast=10,20,30 slow=50,100,200"
```

Only pick parameters that do well on **both** the train and test columns.

### 3. Paper trade (live prices, fake money)

```bash
python -m tradingbot paper
python -m tradingbot status             # position, PnL, risk state
```

### 4. Live trade

1. Create an API key on your exchange with **trade permission only**. Never enable withdrawals. Restrict it to your IP if the exchange allows it.
2. `cp .env.example .env` and fill in the key.
3. Optionally test on the exchange testnet first by setting `exchange.sandbox: true`.
4. Run:

```bash
python -m tradingbot live --confirm-live
```

Run it on an always-on machine (VPS), for example with Docker:

```bash
docker build -t tradingbot .
docker run -d --restart unless-stopped --name tradingbot \
  -v $PWD/config.yaml:/app/config.yaml -v $PWD/.env:/app/.env \
  -v $PWD/state:/app/state -v $PWD/logs:/app/logs \
  tradingbot live --confirm-live
```

## Controlling a running bot

| Action | How |
|---|---|
| Stop gracefully (keeps position) | Ctrl-C / `docker stop` |
| Flatten position and stop | `touch state/STOP`. Delete the file before restarting. |
| Inspect | `python -m tradingbot status`, `logs/tradingbot.log` |
| Resume after the kill switch fired | Review what happened, then `python -m tradingbot reset-halt` |

## How it works

```
every poll_seconds:
  price  -> update equity -> circuit breakers (max drawdown, daily loss)
         -> stop-loss / take-profit check
  new closed candle? -> strategy target (0/1) -> trail stop -> rebalance
  save state
```

| Module | Role |
|---|---|
| `tradingbot/strategies/` | signal generation (`target_positions(df) -> 0/1 series`) |
| `tradingbot/risk.py` | sizing, stops, kill switch, daily loss limit |
| `tradingbot/trader.py` | entry/exit logic shared by backtest and live |
| `tradingbot/brokers/` | `PaperBroker` (simulated) and `CcxtBroker` (real exchange) |
| `tradingbot/backtest.py` | backtester and performance metrics |
| `tradingbot/engine.py` | live/paper loop with state persistence |
| `tradingbot/optimize.py` | parameter grid search with train/test split |

In live mode, equity is counted as *free quote balance + the position the bot opened*. Coins
you already hold in the account are never sold by the bot.

## Writing your own strategy

```python
# tradingbot/strategies/breakout.py
from .base import Strategy

class Breakout(Strategy):
    name = "breakout"
    default_params = {"lookback": 55, "exit_lookback": 20}

    @property
    def warmup_bars(self):
        return self.params["lookback"] + 1

    def target_positions(self, df):
        hi = df["high"].rolling(self.params["lookback"]).max().shift(1)
        lo = df["low"].rolling(self.params["exit_lookback"]).min().shift(1)
        ...  # return a 0/1 Series using only data up to each bar
```

Register it in `tradingbot/strategies/__init__.py`, then set `strategy.name: breakout` in
`config.yaml`. `tests/test_strategies.py` automatically checks every registered
strategy for look-ahead bias.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```
