"""Command line interface: python -m tradingbot <command> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, load_config


def setup_logging(log_dir: str | None = None, verbose: bool = False, quiet: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(log_dir) / "tradingbot.log"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


STOCK_SOURCES = ("trading212", "yahoo")


def _public_exchange(cfg: Config):
    """Market data source: Yahoo Finance for stocks, otherwise the ccxt exchange."""
    if cfg.exchange.name in STOCK_SOURCES:
        from .yahoo import YahooData
        mapping = {cfg.exchange.symbol: cfg.trading212.data_symbol} if cfg.trading212.data_symbol else None
        return YahooData(mapping)
    from .brokers.ccxt_broker import make_exchange
    return make_exchange(cfg.exchange.name, sandbox=cfg.exchange.sandbox)


def _t212_broker(cfg: Config, data):
    import os

    from .brokers.trading212 import Trading212Broker, Trading212Client

    key, secret = os.environ.get("T212_API_KEY"), os.environ.get("T212_API_SECRET")
    if not (key and secret):
        raise SystemExit("Set T212_API_KEY and T212_API_SECRET (see .env.example)")
    t = cfg.trading212
    client = Trading212Client(key, secret, t.environment)
    return Trading212Broker(client, cfg.exchange.symbol, data, t.extended_hours, t.quantity_decimals, t.order_timeout)


def cmd_backtest(args, cfg: Config) -> int:
    from .backtest import format_stats, run_backtest
    from .data import fetch_history, load_csv, synthetic_ohlcv
    from .strategies import create_strategy

    if args.csv:
        df = load_csv(args.csv)
    elif args.synthetic:
        df = synthetic_ohlcv(bars=args.synthetic)
    else:
        if not args.since:
            print("Provide --csv FILE, --synthetic N, or --since DATE to download data", file=sys.stderr)
            return 2
        df = fetch_history(_public_exchange(cfg), cfg.exchange.symbol, cfg.exchange.timeframe, args.since, args.until)

    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    result = run_backtest(df, strategy, cfg)
    print(f"\nBacktest: {strategy!r} on {cfg.exchange.symbol} {cfg.exchange.timeframe} ({len(df)} bars)")
    print(format_stats(result.stats))
    if args.trades_out:
        result.trades_frame().to_csv(args.trades_out, index=False)
        print(f"\nTrades written to {args.trades_out}")
    if args.equity_out:
        result.equity.to_csv(args.equity_out)
        print(f"Equity curve written to {args.equity_out}")
    return 0


def cmd_optimize(args, cfg: Config) -> int:
    from .data import load_csv, synthetic_ohlcv
    from .optimize import grid_search, parse_grid

    df = load_csv(args.csv) if args.csv else synthetic_ohlcv(bars=args.synthetic or 3000)
    results = grid_search(df, cfg, cfg.strategy.name, parse_grid(args.grid), metric=args.metric, train_frac=args.train_frac)
    print(results.head(args.top).to_string(index=False))
    return 0


def cmd_download(args, cfg: Config) -> int:
    from .data import fetch_history, save_csv

    df = fetch_history(_public_exchange(cfg), cfg.exchange.symbol, cfg.exchange.timeframe, args.since, args.until)
    save_csv(df, args.out)
    print(f"Saved {len(df)} candles to {args.out}")
    return 0


def cmd_run(args, cfg: Config) -> int:
    from .brokers.paper import PaperBroker
    from .engine import Engine
    from .notify import make_notifier
    from .strategies import create_strategy

    cfg.engine.mode = args.command
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    log = logging.getLogger("tradingbot")
    data = _public_exchange(cfg)

    if args.command == "live":
        if cfg.exchange.name == "trading212":
            real_money = cfg.trading212.environment == "live"
            label = f"t212-{cfg.trading212.environment}"
        else:
            real_money = not cfg.exchange.sandbox
            label = f"live-{cfg.exchange.name}" + ("" if real_money else "-sandbox")
        if real_money and not args.confirm_live:
            print("Refusing to trade real money without --confirm-live", file=sys.stderr)
            return 2
        if cfg.exchange.name == "trading212":
            broker = _t212_broker(cfg, data)
        elif cfg.exchange.name == "yahoo":
            print("Yahoo is a data source only; use 'paper' mode or exchange.name: trading212", file=sys.stderr)
            return 2
        else:
            from .brokers.ccxt_broker import CcxtBroker, make_exchange
            if not (cfg.api_key and cfg.api_secret):
                print("Set EXCHANGE_API_KEY and EXCHANGE_API_SECRET (see .env.example)", file=sys.stderr)
                return 2
            data = make_exchange(cfg.exchange.name, cfg.api_key, cfg.api_secret, cfg.api_password, cfg.exchange.sandbox)
            broker = CcxtBroker(data, cfg.exchange.symbol)
        if real_money:
            log.warning("LIVE TRADING WITH REAL MONEY on %s", cfg.exchange.name)
        else:
            log.warning("Trading on a DEMO/SANDBOX account (%s)", label)
    else:
        label = f"paper-{cfg.exchange.name}"
        from .yahoo import YahooData

        # broker.symbol, not the config symbol: the scanner can switch stocks.
        if isinstance(data, YahooData):
            def price_source() -> float:
                return data.last_price(broker.symbol)
        else:
            from .brokers.ccxt_broker import with_retries

            def price_source() -> float:
                return float(with_retries(data.fetch_ticker, broker.symbol)["last"])

        broker = PaperBroker(cfg.exchange.symbol, cfg.backtest.initial_cash, cfg.backtest.fee_rate,
                             cfg.backtest.slippage, price_source=price_source)

    scanner = None
    if cfg.scanner.enabled:
        if cfg.exchange.name not in STOCK_SOURCES:
            print("The stock scanner needs exchange.name: trading212 (or yahoo for paper)", file=sys.stderr)
            return 2
        from .scanner import DEFAULT_UNIVERSE, Scanner
        universe = cfg.scanner.universe or DEFAULT_UNIVERSE
        if hasattr(broker, "tradable"):
            universe = broker.tradable(universe)
        if not universe:
            print("Scanner universe is empty", file=sys.stderr)
            return 2
        scanner = Scanner(data, strategy, cfg.scanner, cfg.exchange.timeframe, cfg.engine.history_bars, universe)
        log.info("Scanner on: picking the best of %d stocks", len(universe))

    notifier = make_notifier(cfg, prefix=f"[{label}] ")
    engine = Engine(cfg, strategy, broker, data, notifier=notifier, label=label, scanner=scanner)
    engine.run(max_iterations=args.iterations)
    return 0


def cmd_t212_check(args, cfg: Config) -> int:
    """Verify Trading 212 credentials, ticker and price feed; optionally round-trip a tiny demo trade."""
    if cfg.exchange.name != "trading212":
        print("Set exchange.name: trading212 in your config (see config.trading212.example.yaml)", file=sys.stderr)
        return 2
    data = _public_exchange(cfg)
    broker = _t212_broker(cfg, data)
    summary = broker.account_summary()
    symbol = cfg.exchange.symbol
    print(f"Environment:     {cfg.trading212.environment.upper()}")
    print(f"Account:         id {summary.get('id')} · currency {broker.account_currency}")
    print(f"Cash available:  {(summary.get('cash') or {}).get('availableToTrade')} {broker.account_currency}")
    print(f"Total value:     {summary.get('totalValue')} {broker.account_currency}")
    print(f"Instrument:      {symbol} · {broker.instrument.get('name')} · {broker.instrument_currency} "
          f"· type {broker.instrument.get('type')}")
    print(f"Price feed:      Yahoo '{data.resolve(symbol)}' = {broker.last_price()} {data.currency(symbol)}")
    fx = broker._fx()
    print(f"FX:              1 {broker.account_currency} = {fx:.4f} {broker.instrument_currency}")
    market_open = data.is_market_open(symbol)
    print(f"Market open:     {market_open}")
    print(f"Position held:   {broker.position_qty()}")
    cash, _ = broker.balances()
    print(f"Buying power:    {cash:,.2f} {broker.instrument_currency}")

    if args.test_trade:
        if cfg.trading212.environment != "demo":
            print("--test-trade is only allowed on the demo environment", file=sys.stderr)
            return 2
        if not market_open:
            print("Market is closed; the test order would be queued. Try again during market hours.", file=sys.stderr)
            return 2
        print(f"\nTest trade: BUY {args.qty} then SELL it back ...")
        buy = broker.market_buy(args.qty)
        print(f"  bought {buy.qty} @ {buy.price} (order {buy.order_id})")
        sell = broker.market_sell(buy.qty)
        print(f"  sold   {sell.qty} @ {sell.price} (order {sell.order_id})")
        print("Demo round trip OK")
    print("\nTrading 212 connection OK")
    return 0


def cmd_scan(args, cfg: Config) -> int:
    """Rank the scanner universe right now (no trading)."""
    from .scanner import DEFAULT_UNIVERSE, Scanner, format_table
    from .strategies import create_strategy
    from .yahoo import YahooData

    universe = args.symbols.split(",") if args.symbols else (cfg.scanner.universe or DEFAULT_UNIVERSE)
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    scanner = Scanner(YahooData(), strategy, cfg.scanner, cfg.exchange.timeframe, cfg.engine.history_bars, universe)
    print(f"Scanning {len(universe)} stocks on {cfg.exchange.timeframe} candles "
          f"({cfg.scanner.lookback_bars}-bar momentum, {strategy!r}) ...", flush=True)
    results = scanner.scan()
    if not results:
        print("No data for any ticker", file=sys.stderr)
        return 1
    print(format_table(results, cfg.scanner.min_price, args.top))
    best = scanner.best()
    print(f"\nBest pick: {best.symbol} (score {best.score:+.2f}, {best.momentum:+.1%})" if best
          else "\nNo stock is eligible right now (none has a buy signal with positive momentum)")
    return 0


def cmd_telegram_test(args, cfg: Config) -> int:
    import os

    from .notify import TelegramNotifier

    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN (create a bot with @BotFather)", file=sys.stderr)
        return 2
    if not chat_id:
        n = TelegramNotifier(token, "0")
        updates = n._call("getUpdates")
        chats = {str(u["message"]["chat"]["id"]): u["message"]["chat"].get("username") or
                 u["message"]["chat"].get("title") for u in updates if "message" in u}
        n.close()
        if not chats:
            print("No messages found. Send any message to your bot in Telegram, then run this again.")
            return 1
        for cid, name in chats.items():
            print(f"Found chat: TELEGRAM_CHAT_ID={cid}  ({name})")
        print("Put the right one in your .env and run telegram-test again.")
        return 0
    n = TelegramNotifier(token, chat_id)
    n.send_now("✅ Trading bot test message: Telegram alerts are working.")
    n.close()
    print("Test message sent")
    return 0


def cmd_setup(args, cfg: Config) -> int:
    """Interactive first-time setup: writes .env and config.yaml for Trading 212 demo."""
    import getpass
    import shutil

    env_path = Path(".env")
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    print("Trading 212 API credentials (input is hidden; press Enter to keep the current value)")
    for key, label in (("T212_API_KEY", "API key"), ("T212_API_SECRET", "API secret")):
        value = getpass.getpass(f"  {label}: ").strip()
        if value:
            env[key] = value
    print("Telegram (optional, press Enter to skip)")
    token = getpass.getpass("  Bot token from @BotFather: ").strip()
    if token:
        env["TELEGRAM_BOT_TOKEN"] = token
    chat = input("  Chat id (leave empty if you don't know it yet): ").strip()
    if chat:
        env["TELEGRAM_CHAT_ID"] = chat

    env_path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    print(f"Saved {env_path.resolve()}")

    if not Path("config.yaml").exists():
        shutil.copy("config.trading212.example.yaml", "config.yaml")
        print("Created config.yaml (Trading 212 DEMO, stock scanner on)")
    else:
        print("config.yaml already exists, left unchanged")
    print("\nNext: python -m tradingbot t212-check")
    return 0


def cmd_status(args, cfg: Config) -> int:
    files = sorted(Path(cfg.engine.state_dir).glob("*.json"))
    if not files:
        print("No state files found")
        return 0
    for f in files:
        d = json.loads(f.read_text())
        slots = d.get("slots") or [{"symbol": d.get("symbol"), "trader": d["trader"]}]
        trades = [t for s in slots for t in s["trader"]["trades"]]
        pnl = sum(t["pnl"] for t in trades)
        print(f"== {f.name} (updated {d.get('updated')})")
        held = [s for s in slots if s["trader"]["position"]["qty"] > 0]
        for s in held:
            p = s["trader"]["position"]
            print(f"   position: {s.get('symbol')} qty {p['qty']:g} @ {p['entry_price']:.4f} · stop {p['stop']:.4f}")
        if not held:
            print("   position: flat")
        print(f"   risk:     {json.dumps(d['risk'])}")
        print(f"   trades:   {len(trades)}  realised PnL: {pnl:.2f}")
        if "paper" in d:
            print(f"   paper:    {json.dumps(d['paper'])}")
    return 0


def cmd_reset_halt(args, cfg: Config) -> int:
    files = sorted(Path(cfg.engine.state_dir).glob("*.json"))
    for f in files:
        d = json.loads(f.read_text())
        if d["risk"].get("halted"):
            d["risk"].update(halted=False, halt_reason="", peak_equity=0.0)
            f.write_text(json.dumps(d, indent=2))
            print(f"Cleared kill switch in {f.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingbot", description="Automated crypto trading bot")
    p.add_argument("-c", "--config", default=None, help="YAML config file (default: config.yaml if present)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backtest", help="Backtest the configured strategy")
    b.add_argument("--csv", help="OHLCV CSV (timestamp,open,high,low,close,volume)")
    b.add_argument("--synthetic", type=int, metavar="BARS", help="Use N bars of synthetic data")
    b.add_argument("--since", help="Download data from this date (e.g. 2023-01-01)")
    b.add_argument("--until", help="Download data up to this date")
    b.add_argument("--trades-out", help="Write trades CSV")
    b.add_argument("--equity-out", help="Write equity curve CSV")

    o = sub.add_parser("optimize", help="Grid-search strategy params with a train/test split")
    o.add_argument("--csv")
    o.add_argument("--synthetic", type=int, metavar="BARS")
    o.add_argument("--grid", required=True, help='e.g. "fast=10,20,30 slow=50,100"')
    o.add_argument("--metric", default="sharpe")
    o.add_argument("--train-frac", type=float, default=0.7)
    o.add_argument("--top", type=int, default=10)

    d = sub.add_parser("download", help="Download historical candles to CSV")
    d.add_argument("--since", required=True)
    d.add_argument("--until")
    d.add_argument("--out", required=True)

    for name, help_ in (("paper", "Trade live market data with a simulated account"),
                        ("live", "Trade on a real account, or a demo/sandbox account")):
        r = sub.add_parser(name, help=help_)
        r.add_argument("--iterations", type=int, default=None, help="Stop after N loop iterations")
        if name == "live":
            r.add_argument("--confirm-live", action="store_true",
                           help="Required for real-money accounts (not for Trading 212 demo / exchange sandbox)")

    t = sub.add_parser("t212-check", help="Check the Trading 212 connection (and optionally place a demo test trade)")
    t.add_argument("--test-trade", action="store_true", help="Buy and immediately sell --qty shares (demo only)")
    t.add_argument("--qty", type=float, default=0.1)

    s = sub.add_parser("scan", help="Rank the stock scanner universe and show the best pick (no trading)")
    s.add_argument("--top", type=int, default=20, help="Rows to show")
    s.add_argument("--symbols", help="Comma-separated tickers instead of the configured universe")

    sub.add_parser("setup", help="First-time setup: enter your API keys, creates .env and config.yaml")
    sub.add_parser("telegram-test", help="Send a Telegram test message (or discover your chat id)")
    sub.add_parser("status", help="Show saved bot state")
    sub.add_parser("reset-halt", help="Clear a triggered max-drawdown kill switch")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or ("config.yaml" if Path("config.yaml").exists() else None)
    cfg = load_config(config_path)
    trading = args.command in ("paper", "live", "t212-check")
    # Backtests log every trade at INFO; only show that with -v.
    setup_logging(cfg.engine.log_dir if args.command in ("paper", "live") else None, args.verbose, quiet=not trading)
    handlers = {
        "backtest": cmd_backtest, "optimize": cmd_optimize, "download": cmd_download,
        "paper": cmd_run, "live": cmd_run, "status": cmd_status, "reset-halt": cmd_reset_halt,
        "t212-check": cmd_t212_check, "scan": cmd_scan, "setup": cmd_setup, "telegram-test": cmd_telegram_test,
    }
    return handlers[args.command](args, cfg)
