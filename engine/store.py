"""Trade state storage.

MemoryStore keeps everything in dicts (tests, simulator). JsonStore persists the
trades to a file (phase 1). SqliteStore persists trades, presets, events and
settings in one SQLite file (phase 2, used by the API server).

The bot saves after every change so a restart never fires a step twice.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .models import Preset, Trade, TradeSettings


class MemoryStore:
    def __init__(self) -> None:
        self.trades: dict[int, Trade] = {}
        self.presets: dict[int, Preset] = {}
        self.events: list[dict] = []
        self.settings: dict[str, dict] = {}
        self.channels: dict[int, dict] = {}
        self.signals: list[dict] = []
        self._next_preset = 1

    # -- trades

    def save(self, trade: Trade) -> None:
        self.trades[trade.ticket] = trade
        self._flush()

    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades.values() if t.open]

    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades.values() if not t.open]

    def _flush(self) -> None:
        pass

    # -- presets

    def list_presets(self) -> list[Preset]:
        return sorted(self.presets.values(), key=lambda p: p.id or 0)

    def save_preset(self, preset: Preset) -> Preset:
        if preset.id is None:
            preset.id = self._next_preset
            self._next_preset += 1
        self.presets[preset.id] = preset
        return preset

    def delete_preset(self, preset_id: int) -> bool:
        return self.presets.pop(preset_id, None) is not None

    # -- events (the alert feed)

    def add_event(self, event: dict) -> dict:
        event = dict(event)
        event.setdefault("ts", time.time())
        event["id"] = len(self.events) + 1
        self.events.append(event)
        return event

    def recent_events(self, limit: int = 100) -> list[dict]:
        return self.events[-limit:]

    # -- settings (risk limits etc.)

    def get_setting(self, key: str) -> dict | None:
        return self.settings.get(key)

    def put_setting(self, key: str, value: dict) -> None:
        self.settings[key] = value

    # -- telegram channels (dicts, see signals.models.Channel)

    def save_channel(self, ch: dict) -> dict:
        self.channels[int(ch["id"])] = ch
        return ch

    def delete_channel(self, channel_id: int) -> bool:
        return self.channels.pop(channel_id, None) is not None

    # -- signals (dicts, see signals.models.Signal)

    def save_signal(self, sig: dict) -> dict:
        if sig.get("id") is None:
            sig["id"] = max((x["id"] for x in self.signals), default=0) + 1
            self.signals.append(sig)
        else:
            self.signals = [sig if x["id"] == sig["id"] else x for x in self.signals]
        return sig

    def recent_signals(self, limit: int = 100, channel_id: int | None = None) -> list[dict]:
        rows = [x for x in self.signals if channel_id is None or x["channel_id"] == channel_id]
        return rows[-limit:]

    def get_signal(self, signal_id: int) -> dict | None:
        return next((x for x in self.signals if x["id"] == signal_id), None)


class JsonStore(MemoryStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.trades = {int(k): Trade.from_dict(v) for k, v in data.items()}

    def _flush(self) -> None:
        data = {str(k): t.to_dict() for k, t in self.trades.items()}
        # Write atomically so a crash mid-write can't corrupt the state file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ticket INTEGER PRIMARY KEY, open INTEGER NOT NULL, opened_at REAL, closed_at REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS presets (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT, ticket INTEGER, message TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel_id INTEGER, status TEXT, data TEXT NOT NULL);
"""


class SqliteStore(MemoryStore):
    """Trades stay cached in memory (the engine reads them every tick); every change is written through."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        for (data,) in self.db.execute("SELECT data FROM trades"):
            t = Trade.from_dict(json.loads(data))
            self.trades[t.ticket] = t
        for pid, name, data in self.db.execute("SELECT id, name, data FROM presets"):
            self.presets[pid] = Preset(name, TradeSettings.from_dict(json.loads(data)), pid)
        for key, data in self.db.execute("SELECT key, data FROM settings"):
            self.settings[key] = json.loads(data)
        for cid, data in self.db.execute("SELECT id, data FROM channels"):
            self.channels[cid] = json.loads(data)

    def save(self, trade: Trade) -> None:
        self.trades[trade.ticket] = trade
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO trades (ticket, open, opened_at, closed_at, data) VALUES (?, ?, ?, ?, ?)",
                (trade.ticket, int(trade.open), trade.opened_at, trade.closed_at, json.dumps(trade.to_dict())))

    def save_preset(self, preset: Preset) -> Preset:
        data = json.dumps(asdict(preset.settings))
        with self._lock:
            if preset.id is None:
                cur = self.db.execute("INSERT INTO presets (name, data) VALUES (?, ?)", (preset.name, data))
                preset.id = cur.lastrowid
            else:
                self.db.execute("INSERT OR REPLACE INTO presets (id, name, data) VALUES (?, ?, ?)",
                                (preset.id, preset.name, data))
        self.presets[preset.id] = preset
        return preset

    def delete_preset(self, preset_id: int) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        return self.presets.pop(preset_id, None) is not None

    def add_event(self, event: dict) -> dict:
        event = dict(event)
        event.setdefault("ts", time.time())
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events (ts, kind, ticket, message, action) VALUES (?, ?, ?, ?, ?)",
                (event["ts"], event.get("kind"), event.get("ticket"), event.get("message"), event.get("action")))
            event["id"] = cur.lastrowid
        return event

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = list(self.db.execute(
                "SELECT id, ts, kind, ticket, message, action FROM events ORDER BY id DESC LIMIT ?", (limit,)))
        out = [dict(zip(("id", "ts", "kind", "ticket", "message", "action"), r)) for r in rows]
        return list(reversed(out))

    def put_setting(self, key: str, value: dict) -> None:
        self.settings[key] = value
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO settings (key, data) VALUES (?, ?)", (key, json.dumps(value)))

    def save_channel(self, ch: dict) -> dict:
        self.channels[int(ch["id"])] = ch
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO channels (id, data) VALUES (?, ?)", (int(ch["id"]), json.dumps(ch)))
        return ch

    def delete_channel(self, channel_id: int) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        return self.channels.pop(channel_id, None) is not None

    def save_signal(self, sig: dict) -> dict:
        with self._lock:
            if sig.get("id") is None:
                cur = self.db.execute("INSERT INTO signals (ts, channel_id, status, data) VALUES (?, ?, ?, ?)",
                                      (sig["ts"], sig["channel_id"], sig["status"], "{}"))
                sig["id"] = cur.lastrowid
            self.db.execute("UPDATE signals SET status = ?, data = ? WHERE id = ?",
                            (sig["status"], json.dumps(sig), sig["id"]))
        return sig

    def recent_signals(self, limit: int = 100, channel_id: int | None = None) -> list[dict]:
        q = "SELECT data FROM signals" + (" WHERE channel_id = ?" if channel_id is not None else "") +             " ORDER BY id DESC LIMIT ?"
        args = (channel_id, limit) if channel_id is not None else (limit,)
        with self._lock:
            rows = list(self.db.execute(q, args))
        return [json.loads(d) for (d,) in reversed(rows) if d]

    def get_signal(self, signal_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT data FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
