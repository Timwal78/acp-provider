"""Backtest engine: causal event loop, immutable data cache, honest statistics.

Public surface:
    data.get_bars(venue, symbol, interval, start, end) -> (bars, meta)
    engine.run(bars, strategy, ...)                    -> Result
    stats.summarize(result, periods_per_year, ...)     -> dict
    data.receipt(meta, config, summary)                -> reproducible receipt
"""
from . import data, engine, feeds, stats, store  # noqa: F401

__all__ = ["data", "engine", "feeds", "stats", "store"]
