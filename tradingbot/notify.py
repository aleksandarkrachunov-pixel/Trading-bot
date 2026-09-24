"""Telegram alerts and remote commands.

Messages are sent from a background thread so a slow or unreachable Telegram
API never delays trading. Commands (/status, /stop, /help) are accepted only
from the configured chat.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    """No-op notifier (Telegram disabled)."""

    enabled = False

    def send(self, text: str, key: str | None = None, throttle: float = 0) -> None:
        pass

    def start_commands(self, handlers: dict[str, Callable[[], str]]) -> None:
        pass

    def close(self, timeout: float = 5.0) -> None:
        pass


class TelegramNotifier(Notifier):
    enabled = True

    def __init__(self, token: str, chat_id: str, prefix: str = "", session: requests.Session | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.prefix = prefix
        self.session = session or requests.Session()
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=200)
        self._last_sent: dict[str, float] = {}
        self._stop = threading.Event()
        self._sender = threading.Thread(target=self._send_loop, name="telegram-send", daemon=True)
        self._sender.start()
        self._listener: threading.Thread | None = None

    def _call(self, method: str, http_timeout: float = 15, **payload):
        r = self.session.post(API.format(token=self.token, method=method), json=payload, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.text[:200])}")
        return data["result"]

    # ---- sending --------------------------------------------------------------------
    def send(self, text: str, key: str | None = None, throttle: float = 0) -> None:
        """Queue a message. With `key` + `throttle`, repeats within `throttle` seconds are dropped."""
        if key and throttle:
            now = time.monotonic()
            if now - self._last_sent.get(key, -1e18) < throttle:
                return
            self._last_sent[key] = now
        try:
            self._queue.put_nowait(f"{self.prefix}{text}")
        except queue.Full:
            log.warning("Telegram queue full, dropping message")

    def send_now(self, text: str) -> None:
        """Synchronous send (used by `telegram-test`); raises on failure."""
        self._call("sendMessage", chat_id=self.chat_id, text=f"{self.prefix}{text}", disable_web_page_preview=True)

    def _send_loop(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            for attempt in range(3):
                try:
                    self._call("sendMessage", chat_id=self.chat_id, text=text[:4000], disable_web_page_preview=True)
                    break
                except Exception as e:  # never let alerting crash the bot
                    log.warning("Telegram send failed (%s)", e)
                    time.sleep(2 ** attempt)

    # ---- commands -------------------------------------------------------------------
    def start_commands(self, handlers: dict[str, Callable[[], str]]) -> None:
        self._handlers = handlers
        self._listener = threading.Thread(target=self._listen_loop, name="telegram-listen", daemon=True)
        self._listener.start()

    def _listen_loop(self) -> None:
        offset = None
        # Skip commands sent while the bot was offline (don't replay an old /stop).
        try:
            pending = self._call("getUpdates", offset=-1)
            if pending:
                offset = pending[-1]["update_id"] + 1
        except Exception as e:
            log.warning("Telegram getUpdates failed (%s)", e)
        while not self._stop.is_set():
            try:
                # Long polling: Telegram holds the request up to 30s until a message arrives.
                updates = self._call("getUpdates", http_timeout=40, offset=offset, timeout=30)
            except Exception as e:
                log.debug("Telegram poll failed (%s)", e)
                self._stop.wait(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self._handle_update(u)

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return  # ignore everyone else
        command = (msg.get("text") or "").strip().split()[0:1]
        if not command:
            return
        name = command[0].split("@")[0].lstrip("/").lower()
        handler = self._handlers.get(name)
        if handler is None:
            self.send("Commands: " + " ".join(f"/{c}" for c in sorted(self._handlers)))
            return
        try:
            self.send(handler())
        except Exception as e:
            self.send(f"Command /{name} failed: {e}")

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        self._sender.join(timeout)


def make_notifier(cfg, prefix: str = "") -> Notifier:
    if not cfg.telegram.enabled:
        return Notifier()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        log.warning("telegram.enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set; alerts disabled")
        return Notifier()
    return TelegramNotifier(token, chat_id, prefix=prefix)
