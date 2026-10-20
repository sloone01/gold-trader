"""TelegramListener's chat-list cache, driven on a real event loop without Telethon or a network."""
import asyncio
import threading
import time

import pytest

from signals import telegram
from signals.telegram import TelegramListener


@pytest.fixture
def listener():
    tg = TelegramListener(1, "hash", "unused.session", on_message=lambda *a: None, channel_ids=[])
    thread = threading.Thread(target=tg.loop.run_forever, daemon=True)
    thread.start()
    tg.connected = True
    yield tg
    tg.loop.call_soon_threadsafe(tg.loop.stop)
    thread.join(2)


def fake_dialogs(tg, delay=0.0):
    calls = []

    async def _dialogs(limit):
        calls.append(limit)
        await asyncio.sleep(delay)
        return [{"id": -100 - len(calls), "title": f"Chan {len(calls)}", "kind": "channel", "username": None, "forum": False}]

    tg._dialogs = _dialogs
    return calls


def test_dialogs_are_cached(listener):
    calls = fake_dialogs(listener)
    first, err = listener.list_dialogs()
    assert err is None and first[0]["title"] == "Chan 1"
    assert listener.list_dialogs() == (first, None) and len(calls) == 1      # served from cache
    fresh, _ = listener.list_dialogs(refresh=True)
    assert fresh[0]["title"] == "Chan 2" and len(calls) == 2


def test_slow_listing_times_out_then_lands_in_cache(listener):
    calls = fake_dialogs(listener, delay=0.3)
    dialogs, err = listener.list_dialogs(timeout=0.05)
    assert dialogs == [] and "still loading" in err
    assert listener.list_dialogs(timeout=0.05)[1] is not None and len(calls) == 1   # no second listing started
    time.sleep(0.4)
    assert listener.list_dialogs() == ([{"id": -101, "title": "Chan 1", "kind": "channel", "username": None, "forum": False}], None)


def test_stale_cache_answers_now_and_refreshes_behind(listener, monkeypatch):
    calls = fake_dialogs(listener)
    listener.list_dialogs()
    monkeypatch.setattr(telegram, "DIALOGS_TTL", -1)
    assert listener.list_dialogs()[0][0]["title"] == "Chan 1"       # old list, immediately
    time.sleep(0.1)
    monkeypatch.setattr(telegram, "DIALOGS_TTL", 600)
    assert listener.list_dialogs()[0][0]["title"] == "Chan 2" and len(calls) == 2


def test_listing_error_is_reported(listener):
    async def boom(limit):
        raise RuntimeError("flood wait")
    listener._dialogs = boom
    dialogs, err = listener.list_dialogs()
    assert dialogs == [] and "flood wait" in err
    assert listener._dialogs_fut is None                             # next call can try again


def test_not_connected_returns_empty(listener):
    listener.connected = False
    assert listener.list_dialogs() == ([], None) and listener.list_topics(-1001) == []
