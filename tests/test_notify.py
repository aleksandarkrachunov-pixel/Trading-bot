import time

from fakes import FakeResponse, FakeSession
from tradingbot.config import Config
from tradingbot.notify import Notifier, TelegramNotifier, make_notifier


def wait_for(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def sent_texts(session):
    return [c[3]["text"] for c in session.calls if c[1].endswith("/sendMessage")]


def make():
    session = FakeSession(lambda *a: FakeResponse(200, {"ok": True, "result": []}))
    return TelegramNotifier("TOKEN", "42", prefix="[t] ", session=session), session


def test_send_and_throttle():
    n, session = make()
    n.send("hello")
    n.send("err", key="E", throttle=60)
    n.send("err again", key="E", throttle=60)  # dropped
    assert wait_for(lambda: len(sent_texts(session)) == 2)
    n.close()
    assert sent_texts(session) == ["[t] hello", "[t] err"]
    assert session.calls[0][1] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert session.calls[0][3]["chat_id"] == "42"


def test_commands_only_from_own_chat():
    n, session = make()
    called = []
    n._handlers = {"status": lambda: "all good", "stop": lambda: called.append(1) or "stopping"}
    n._handle_update({"message": {"chat": {"id": 999}, "text": "/stop"}})
    n._handle_update({"message": {"chat": {"id": 42}, "text": "/status@mybot"}})
    n._handle_update({"message": {"chat": {"id": 42}, "text": "/unknown"}})
    assert wait_for(lambda: len(sent_texts(session)) == 2)
    n.close()
    assert called == []
    assert sent_texts(session)[0] == "[t] all good"
    assert "Commands:" in sent_texts(session)[1]


def test_send_failure_does_not_raise():
    def boom(*a):
        raise ConnectionError("offline")
    n = TelegramNotifier("T", "1", session=FakeSession(boom))
    n.send("x")
    n.close(timeout=0.1)


def test_make_notifier_requires_env(monkeypatch):
    cfg = Config()
    assert type(make_notifier(cfg)) is Notifier
    cfg.telegram.enabled = True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert type(make_notifier(cfg)) is Notifier
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    n = make_notifier(cfg)
    assert isinstance(n, TelegramNotifier)
    n.close()
