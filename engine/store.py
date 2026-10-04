"""Trade state storage. A JSON file for phase 1; SQLite replaces it in phase 2.

The bot saves after every change so a restart never fires a step twice.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Trade


class MemoryStore:
    def __init__(self) -> None:
        self.trades: dict[int, Trade] = {}

    def save(self, trade: Trade) -> None:
        self.trades[trade.ticket] = trade
        self._flush()

    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades.values() if t.open]

    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades.values() if not t.open]

    def _flush(self) -> None:
        pass


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
