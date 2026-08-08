"""Immutable, content-addressed OHLCV store.

Backtests are only citable if the data underneath them is pinned. This store
never mutates a block once written: every block is keyed by
(venue, symbol, interval, start, end) and carries a sha256 of its own bytes,
so a backtest receipt can name the exact data it ran on and anyone can check.

Format is packed binary at 48 bytes/bar (uint64 ts + 5 float64). float32 was
tried first and rejected: it round-trips a $116,728 BTC print to $116,728.367,
an error of 0.003. Numerically that is nothing, but it makes a stored price
differ from the price the venue actually published, which quietly voids the
exactness a signed receipt is supposed to guarantee. float64 is lossless here
and the cost is trivial -- measured, 1 symbol-year of hourly bars is 0.42 MB,
so 50 symbols x 5 years is ~105 MB of the 1 GB volume.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time
from pathlib import Path

FORMAT_VERSION = 2              # bump invalidates cached blocks of older layout
BAR = struct.Struct("<Qddddd")  # ts, open, high, low, close, volume
BAR_BYTES = BAR.size

DEFAULT_ROOT = os.environ.get("BT_CACHE_DIR", "/var/data/bt_cache")
# Hard ceiling well under the 1 GB volume: the beacon reputation ledger shares
# this disk and must never be squeezed out by candle data.
DEFAULT_BUDGET_MB = int(os.environ.get("BT_CACHE_BUDGET_MB", "700"))


def pack_bars(bars) -> bytes:
    out = bytearray()
    for b in bars:
        out += BAR.pack(int(b[0]), float(b[1]), float(b[2]), float(b[3]),
                        float(b[4]), float(b[5]))
    return bytes(out)


def unpack_bars(blob: bytes) -> list[tuple]:
    n = len(blob) // BAR_BYTES
    return [BAR.unpack_from(blob, i * BAR_BYTES) for i in range(n)]


class CandleStore:
    def __init__(self, root: str | None = None, budget_mb: int | None = None):
        self.root = Path(root or DEFAULT_ROOT)
        self.budget = (budget_mb or DEFAULT_BUDGET_MB) * 1_000_000
        self.blocks = self.root / "blocks"
        self.blocks.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.index = self._read_index()

    def _read_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text())
        except Exception:
            return {}

    def _write_index(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index))
        tmp.replace(self.index_path)

    @staticmethod
    def key(venue: str, symbol: str, interval: str, start: int, end: int) -> str:
        raw = f"v{FORMAT_VERSION}|{venue}|{symbol}|{interval}|{start}|{end}".encode()
        return hashlib.sha256(raw).hexdigest()[:32]

    def get(self, venue, symbol, interval, start, end):
        k = self.key(venue, symbol, interval, start, end)
        meta = self.index.get(k)
        if not meta:
            return None
        p = self.blocks / f"{k}.bin"
        if not p.exists():
            self.index.pop(k, None)
            self._write_index()
            return None
        blob = p.read_bytes()
        # Content check: a truncated or corrupted block must fail loudly rather
        # than silently backtest on partial data.
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            p.unlink(missing_ok=True)
            self.index.pop(k, None)
            self._write_index()
            return None
        meta["atime"] = int(time.time())
        self._write_index()
        return unpack_bars(blob)

    def put(self, venue, symbol, interval, start, end, bars, tier="hot"):
        k = self.key(venue, symbol, interval, start, end)
        blob = pack_bars(bars)
        (self.blocks / f"{k}.bin").write_bytes(blob)
        self.index[k] = {
            "venue": venue, "symbol": symbol, "interval": interval,
            "start": start, "end": end, "bars": len(bars),
            "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
            "atime": int(time.time()), "tier": tier,
        }
        self._write_index()
        self.evict()
        return self.index[k]["sha256"]

    def total_bytes(self) -> int:
        return sum(m["bytes"] for m in self.index.values())

    def evict(self) -> int:
        """LRU eviction, but 1-minute blocks go first -- they are ~60x heavier
        per symbol-year and cheapest to refetch for a narrow window."""
        freed = 0
        if self.total_bytes() <= self.budget:
            return 0
        items = sorted(
            self.index.items(),
            key=lambda kv: (0 if kv[1]["interval"] in ("1m", "60") else 1, kv[1]["atime"]),
        )
        for k, m in items:
            if self.total_bytes() <= self.budget:
                break
            (self.blocks / f"{k}.bin").unlink(missing_ok=True)
            freed += m["bytes"]
            self.index.pop(k, None)
        self._write_index()
        return freed

    def stats(self) -> dict:
        return {
            "blocks": len(self.index),
            "bytes": self.total_bytes(),
            "budget_bytes": self.budget,
            "pct_used": round(100 * self.total_bytes() / self.budget, 2) if self.budget else 0,
        }
