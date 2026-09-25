# Trading Bot

A fully automated trading bot in Python. It **backtests**, **paper trades** and
**live trades** crypto on any [ccxt](https://github.com/ccxt/ccxt)-supported exchange (Binance,
Kraken, Coinbase, Bybit, OKX, …) and **stocks/ETFs on Trading 212** (demo or live).
It uses the same strategy, risk and order logic in every mode, so what you backtest is
what runs live. **Telegram** sends you alerts and lets you control the bot from your phone.

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
- **Trading 212:** demo (practice) or live account via the official API, with prices and market hours from Yahoo Finance. Includes a connection check and a demo test trade.
- **Stock scanner:** ranks ~45 large US stocks (or your own list) by risk-adjusted momentum and holds the top picks with a buy signal (up to `max_positions` at once).
- **Telegram:** alerts for trades, the kill switch, errors and a daily status. Control the bot with `/status` and `/stop`.
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

To keep it running around the clock, see [Run it 24/7](#run-it-247).

## Trading 212 (stocks & ETFs)

Trading 212's API works with **Invest** and **Stocks ISA** accounts. Test on the **demo**
(practice money) account first.

1. In the Trading 212 app, switch to your **Practice** account, then open
   *Settings → API (Beta) → Generate API key*. Give it these permissions: account,
   portfolio, orders:read, orders:execute, history:orders and metadata.
2. Run `python -m tradingbot setup` and paste the key and secret when asked (input is hidden). Or put them in `.env` yourself:
   ```
   T212_API_KEY=...
   T212_API_SECRET=...
   ```
3. Use the Trading 212 config:
   ```bash
   cp config.trading212.example.yaml config.yaml   # stock scanner on; see below
   ```
4. Check the connection, then place a tiny test round trip (buy 0.1 share and sell it back).
   The test trade needs the market to be open.
   ```bash
   python -m tradingbot t212-check
   python -m tradingbot t212-check --test-trade --qty 0.1
   ```
5. Backtest on the same stock, then start the bot on the demo account:
   ```bash
   python -m tradingbot backtest --since 2024-10-01     # hourly Yahoo data goes back ~2 years
   python -m tradingbot live                            # demo: no --confirm-live needed
   ```

How it works:
- **Prices:** Trading 212's API has no price data, so candles, last prices, market hours
  and FX rates come from Yahoo Finance. The Yahoo symbol is derived from the ticker
  (`AAPL_US_EQ` → `AAPL`, `VUSAl_EQ` → `VUSA.L`). Set `trading212.data_symbol` if the guess is wrong.
- **Market hours:** the bot only trades while the market is open. Signals from the last bar
  of the day are acted on at the next open, the same way the backtest assumes.
- **Orders:** market orders only. The bot waits up to `order_timeout` seconds for a fill,
  then cancels. It reads the fill price from order history.
- **Currencies:** if your account currency differs from the stock's (e.g. GBP account, USD
  stock), cash is converted with a live FX rate. Trading 212 charges a ~0.15% FX fee, so the
  example config sets `fee_rate: 0.0015` for backtests.
- **Rate limits:** the bot respects Trading 212's per-endpoint limits. Keep `poll_seconds` ≥ 30.
- **Going live:** set `trading212.environment: live`, use a key from your real account, and run
  `python -m tradingbot live --confirm-live`.

### Stock scanner: trade the best stock, not just one

With `scanner.enabled: true` (the default in `config.trading212.example.yaml`), the bot
doesn't stick to `exchange.symbol`. It ranks a list of stocks and trades the best one:

- **Score:** risk-adjusted momentum, i.e. the return over `lookback_bars` candles divided by
  the volatility over the same window. A steady climb beats a choppy one with the same gain.
- **Eligible:** the strategy must currently say "buy" (e.g. fast MA above slow MA), the score
  must be positive and the price at least `min_price`.
- **Several positions:** `max_positions` (3 in the example config) is how many stocks the bot
  holds at once. Each position has its own stop and risks `risk_per_trade` of equity, and is
  capped at `max_position_pct / max_positions` of equity, so 3 positions use at most 95% of the
  account. Cash, the drawdown kill switch and the daily loss limit are shared.
- **Rotation:** while a position slot is free, the bot rescans every `rescan_minutes` and fills
  it with the best eligible stock it doesn't already hold. It holds each stock until the stop,
  the strategy exit or the kill switch closes it, then that slot takes the next pick.
- **Universe:** `scanner.universe` lists Trading 212 tickers. Leave it empty for ~45 large US
  stocks (Apple, Microsoft, Nvidia, Meta, JPMorgan, Eli Lilly, Exxon, ...). Tickers not on
  Trading 212, or in a different currency from `exchange.symbol`, are skipped so equity and
  the drawdown limit stay in one currency. You can write `META_US_EQ`; the bot finds
  Trading 212's `FB_US_EQ`.

```bash
python -m tradingbot scan                     # show today's ranking and the pick (no trading)
python -m tradingbot scan --symbols NVDA_US_EQ,AMD_US_EQ,INTC_US_EQ
```

On Telegram, `/scan` shows the latest top 5. Backtests still test one stock
(`exchange.symbol`), so backtest a few of the top picks before trusting the scanner.

## Telegram alerts & remote control

1. In Telegram, message **@BotFather** → `/newbot`, and copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot, then run `python -m tradingbot telegram-test`. It prints your
   chat id. Add it to `.env` as `TELEGRAM_CHAT_ID`.
3. Run `python -m tradingbot telegram-test` again. You should receive a test message.
4. Set `telegram.enabled: true` in `config.yaml`.

You'll get messages for:
- bot start and stop
- every buy and sell, with PnL
- entries blocked by the daily loss limit
- the kill switch firing
- errors (at most one per error type every 30 minutes)
- a status summary every `heartbeat_hours`

Commands (accepted only from your own chat):

| Command | Effect |
|---|---|
| `/status` | equity, price, position, stop, PnL, market open/closed |
| `/stop` | close the position and shut the bot down (for stocks, at the next market open) |
| `/scan` | latest stock-scanner ranking (top 5), when the scanner is on |
| `/help` | list commands |

## Run it 24/7

The bot has to keep running to watch its stop-loss: the stop is checked by the bot, not
placed as an order at the broker. Run it on a machine that stays on: a small Linux VPS
(1 CPU / 1 GB RAM is enough), a Raspberry Pi, or a home server.

**Linux (Ubuntu/Debian), one command:**

```bash
git clone -b claude/jolly-franklin-a9obyc https://github.com/aleksandarkrachunov-pixel/Trading-bot.git
cd Trading-bot && ./deploy/install.sh
```

It installs Docker if needed, asks for your Trading 212 key (and optional Telegram token),
creates `config.yaml` from the Trading 212 example, checks the connection and starts the bot.

**Any machine with Docker (Linux, macOS, Windows with Docker Desktop):**

```bash
cp .env.example .env                       # fill in T212_API_KEY / T212_API_SECRET
cp config.trading212.example.yaml config.yaml
docker compose up -d --build
```

`docker-compose.yml` restarts the bot after a crash or a reboot, keeps `state/` and `logs/`
on the host, and caps Docker's log size. The container reports **unhealthy** if the bot stops
updating its state file for 15 minutes.

| Task | Command |
|---|---|
| Watch the log | `docker compose logs -f` |
| Health / running? | `docker compose ps` |
| Position and PnL | `docker compose run --rm bot status` |
| Today's stock ranking | `docker compose run --rm bot scan` |
| Stop (keeps the position) | `docker compose stop` |
| Sell and stop | `touch state/STOP` (or `/stop` on Telegram) |
| Update to the latest code | `git pull && docker compose up -d --build` |

Set up Telegram (see below) on an always-on machine: it is how you hear about trades,
errors and the kill switch without logging in.

### Lost state file? The bot takes over what the account holds

With `trading212.adopt_positions: true` (on in the Trading 212 example config), the bot checks
at startup for stocks the account holds that it isn't tracking (lost state file, or a crash
right after a fill) and takes them over, as long as they're in the scanner universe (or are
`exchange.symbol`) and a position slot is free. It uses
Trading 212's average price as the entry and rebuilds the stop from ATR, then tells you on
Telegram. Turn it off if you also hold stocks by hand in that account: the bot would manage,
and eventually sell, the ones it takes over.

### Moving a running bot to another machine

The open position lives in `state/*.json`. To move the bot without losing track of it:

1. Stop the old bot gracefully (Ctrl-C / `docker compose stop`, **not** `touch state/STOP`,
   which sells). Best done while the market is closed.
2. Copy the `state/` folder (plus `config.yaml` and `.env`) to the new machine.
3. Start the new bot. The log shows `Resumed state ... (position qty=...)`.

Never run two copies against the same account: both would trade it. Without the state file
the new bot thinks it is flat, can buy another stock, and stops managing the old position.

## Controlling a running bot

| Action | How |
|---|---|
| Stop gracefully (keeps position) | Ctrl-C / `docker stop` |
| Flatten position and stop | `touch state/STOP` or `/stop` on Telegram. Delete `state/STOP` before restarting. |
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
| `tradingbot/brokers/` | `PaperBroker` (simulated), `CcxtBroker` (crypto exchanges), `Trading212Broker` |
| `tradingbot/yahoo.py` | Yahoo Finance candles, prices, market hours, FX (for stocks) |
| `tradingbot/scanner.py` | ranks a stock universe and picks the best one to trade |
| `tradingbot/notify.py` | Telegram alerts and `/status` / `/stop` commands |
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
