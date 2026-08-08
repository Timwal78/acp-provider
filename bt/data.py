"""The data facade every backtest should call.

Exists because of a bug worth remembering: keying a cache block on a raw
wall-clock range means `end=now()` produces a different key on every call, so
the cache never hits and the service silently re-ingests from the venue on
every run -- the exact rate-limit behaviour the cache was built to avoid.

Ranges are therefore snapped down to interval boundaries before hashing, so
repeated runs within the same bar reuse one block.
"""
from __future__ import annotations

import hashlib

from .store import CandleStore
from . import feeds

SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def align(ts: int, interval: str) -> int:
    step = SECONDS.get(interval, 3600)
    return (int(ts) // step) * step


def get_bars(venue, symbol, interval, start, end, store: CandleStore | None = None,
             allow_fetch: bool = True):
    """Return (bars, meta). meta carries the data hash used in the receipt."""
    store = store or CandleStore()
    s, e = align(start, interval), align(end, interval)
    if e <= s:
        raise ValueError("empty range after alignment")

    bars = store.get(venue, symbol, interval, s, e)
    source = "cache"
    if bars is None:
        if not allow_fetch:
            raise LookupError("not cached and fetching disabled")
        bars = feeds.fetch(venue, symbol, interval, s, e)
        if not bars:
            raise RuntimeError(f"{venue} returned no bars for {symbol} {interval}")
        store.put(venue, symbol, interval, s, e, bars)
        source = venue

    k = store.key(venue, symbol, interval, s, e)
    meta = {
        "venue": venue, "symbol": symbol, "interval": interval,
        "start": s, "end": e, "bars": len(bars), "source": source,
        "data_sha256": store.index.get(k, {}).get("sha256"),
        "first_ts": bars[0][0], "last_ts": bars[-1][0],
    }
    return bars, meta


def receipt(meta: dict, config: dict, summary: dict) -> dict:
    """A backtest result nobody has to take on trust.

    Hashes data + config + result together. Re-running with the same inputs
    must reproduce the same digest; if it does not, something changed and the
    earlier claim no longer stands.
    """
    def h(d):
        import json
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()
    return {
        "data": meta,
        "config": config,
        "result": summary,
        "data_hash": meta.get("data_sha256"),
        "config_hash": h(config),
        "result_hash": h(summary),
        "receipt_id": h({"d": meta.get("data_sha256"), "c": h(config), "r": h(summary)})[:32],
    }
