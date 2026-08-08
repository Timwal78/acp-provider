#!/usr/bin/env python3
"""Paid backtest endpoints.

Two rules shape this module.

1. No user-supplied code is ever executed. Strategies are a fixed, named set
   with numeric parameters. A backtest service that evals uploaded strategy
   code is a remote code execution endpoint with extra steps.

2. Everything is bounded. Gunicorn here runs --workers 2 --timeout 120, so an
   unbounded sweep does not fail gracefully -- it hangs a worker, and with two
   workers that is half the service. Bar counts and sweep sizes are capped and
   the caps are reported in the response rather than silently applied.

Strategies keep rolling state instead of recomputing a window every bar. The
engine calls a strategy exactly once per bar in order, which makes incremental
sums valid and turns an O(bars * window) sweep into O(bars).
"""
from __future__ import annotations

import os
import time

from bt import data as bt_data
from bt import engine as bt_engine
from bt import stats as bt_stats
from bt.store import CandleStore

MAX_BARS = 20_000
MAX_SWEEP = 60
PERIODS = {"1m": 525_600, "5m": 105_120, "15m": 35_040, "30m": 17_520,
           "1h": 8_760, "6h": 1_460, "1d": 252}


def _store() -> CandleStore:
    """Prefer the mounted volume; fall back to /tmp rather than 500 if the
    volume is missing or read-only (a cache miss is degraded, not fatal)."""
    for root in (os.environ.get("BT_CACHE_DIR"), "/var/data/bt_cache", "/tmp/bt_cache"):
        if not root:
            continue
        try:
            s = CandleStore(root=root)
            probe = s.blocks / ".w"
            probe.write_text("1")
            probe.unlink()
            return s
        except Exception:
            continue
    raise RuntimeError("no writable cache directory")


# ---------------------------------------------------------------- strategies

def _ma_cross(fast: int, slow: int, ema: bool = False):
    st = {"buf": [], "sf": 0.0, "ss": 0.0, "ef": None, "es": None}
    kf, ks = 2.0 / (fast + 1), 2.0 / (slow + 1)

    def strat(w):
        c = w.close[w.i]
        if ema:
            st["ef"] = c if st["ef"] is None else (c - st["ef"]) * kf + st["ef"]
            st["es"] = c if st["es"] is None else (c - st["es"]) * ks + st["es"]
            if w.i < slow:
                return 0.0
            return 1.0 if st["ef"] > st["es"] else -1.0
        buf = st["buf"]
        buf.append(c)
        st["sf"] += c
        st["ss"] += c
        if len(buf) > fast:
            st["sf"] -= buf[-fast - 1]
        if len(buf) > slow:
            st["ss"] -= buf[-slow - 1]
        if len(buf) < slow:
            return 0.0
        return 1.0 if st["sf"] / fast > st["ss"] / slow else -1.0
    return strat


def _breakout(lookback: int):
    st = {"h": [], "l": []}

    def strat(w):
        i = w.i
        st["h"].append(w.high[i])
        st["l"].append(w.low[i])
        if i < lookback:
            return 0.0
        hh = max(st["h"][-lookback - 1:-1])
        ll = min(st["l"][-lookback - 1:-1])
        c = w.close[i]
        if c > hh:
            return 1.0
        if c < ll:
            return -1.0
        return 0.0
    return strat


def _rsi_reversion(period: int, low: float = 30.0, high: float = 70.0):
    st = {"prev": None, "ag": 0.0, "al": 0.0, "n": 0}

    def strat(w):
        c = w.close[w.i]
        p = st["prev"]
        st["prev"] = c
        if p is None:
            return 0.0
        ch = c - p
        g, l = max(ch, 0.0), max(-ch, 0.0)
        st["n"] += 1
        if st["n"] <= period:
            st["ag"] += g / period
            st["al"] += l / period
            return 0.0
        st["ag"] = (st["ag"] * (period - 1) + g) / period
        st["al"] = (st["al"] * (period - 1) + l) / period
        if st["al"] == 0:
            return -1.0
        rsi = 100.0 - 100.0 / (1.0 + st["ag"] / st["al"])
        if rsi < low:
            return 1.0
        if rsi > high:
            return -1.0
        return 0.0
    return strat


STRATEGIES = {
    "buy_hold": (lambda **k: (lambda w: 1.0), {}),
    "ma_cross": (lambda fast=24, slow=96, **k: _ma_cross(int(fast), int(slow)),
                 {"fast": 24, "slow": 96}),
    "ema_cross": (lambda fast=12, slow=48, **k: _ma_cross(int(fast), int(slow), ema=True),
                  {"fast": 12, "slow": 48}),
    "breakout": (lambda lookback=48, **k: _breakout(int(lookback)), {"lookback": 48}),
    "rsi_reversion": (lambda period=14, low=30, high=70, **k:
                      _rsi_reversion(int(period), float(low), float(high)),
                      {"period": 14, "low": 30, "high": 70}),
}


def _load(params):
    venue = str(params.get("venue", "coinbase"))
    symbol = str(params.get("symbol", "BTC-USD"))
    interval = str(params.get("interval", "1h"))
    days = int(params.get("days", 365))
    warnings = []

    per_year = PERIODS.get(interval)
    if per_year is None:
        raise ValueError(f"unsupported interval {interval}; use {sorted(PERIODS)}")

    step = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "6h": 21600, "1d": 86400}[interval]
    if days * 86400 / step > MAX_BARS:
        capped = int(MAX_BARS * step / 86400)
        warnings.append(f"days reduced {days}->{capped} to stay under the "
                        f"{MAX_BARS}-bar synchronous limit")
        days = max(capped, 1)

    now = int(time.time())
    try:
        bars, meta = bt_data.get_bars(venue, symbol, interval,
                                      now - days * 86400, now, store=_store())
    except RuntimeError as e:
        if "returned no bars" in str(e):
            raise ValueError(
                f"no data for symbol '{symbol}' on {venue} at {interval}. "
                f"Symbol formats differ by venue: coinbase BTC-USD, "
                f"kraken XXBTZUSD, yahoo SPY.") from e
        raise
    if len(bars) < 50:
        raise ValueError(f"only {len(bars)} bars returned for {symbol} {interval}")
    return bars, meta, per_year, warnings


def _run_one(bars, name, sparams, params):
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name}; use {sorted(STRATEGIES)}")
    factory, _ = STRATEGIES[name]
    return bt_engine.run(
        bars, factory(**sparams),
        cash=float(params.get("cash", 10_000)),
        fee_bps=float(params.get("fee_bps", 10)),
        slippage_bps=float(params.get("slippage_bps", 5)),
        allow_short=str(params.get("allow_short", "true")).lower() != "false",
        rebalance_threshold=float(params.get("rebalance_threshold", 0.02)),
    )


def api_backtest_run(params: dict) -> dict:
    t0 = time.time()
    bars, meta, per_year, warnings = _load(params)
    name = str(params.get("strategy", "ma_cross"))
    _, defaults = STRATEGIES.get(name, (None, {}))
    sparams = {k: params.get(k, v) for k, v in defaults.items()}

    r = _run_one(bars, name, sparams, params)
    summary = bt_stats.summarize(r, per_year, n_trials=1,
                                 cash=float(params.get("cash", 10_000)))
    cfg = {"strategy": name, "params": sparams,
           "fee_bps": params.get("fee_bps", 10),
           "slippage_bps": params.get("slippage_bps", 5),
           "allow_short": params.get("allow_short", True)}
    return {
        "ok": True,
        "data": meta,
        "config": cfg,
        "result": summary,
        "receipt": bt_data.receipt(meta, cfg, summary),
        "warnings": warnings,
        "note": ("deflated_sharpe here assumes ONE configuration was tried. If "
                 "you are comparing variants, use backtest-sweep, which "
                 "corrects for how many you tested."),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def api_backtest_sweep(params: dict) -> dict:
    """Sweep a parameter grid and report the winner with its overfitting risk.

    The point of this endpoint is the correction: the best of N tries has an
    inflated Sharpe by construction, and the Deflated Sharpe computed from the
    observed spread across all N is the number that should drive a decision.
    """
    t0 = time.time()
    bars, meta, per_year, warnings = _load(params)
    name = str(params.get("strategy", "ma_cross"))
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name}; use {sorted(STRATEGIES)}")
    _, defaults = STRATEGIES[name]

    grid = {}
    for k, dv in defaults.items():
        raw = params.get(k, dv)
        vals = [v.strip() for v in str(raw).split(",") if v.strip() != ""]
        grid[k] = [float(v) if "." in v else int(v) for v in vals]

    combos = [{}]
    for k, vals in grid.items():
        combos = [dict(c, **{k: v}) for c in combos for v in vals]
    if name in ("ma_cross", "ema_cross"):
        combos = [c for c in combos if c.get("fast", 0) < c.get("slow", 1)]
    if not combos:
        raise ValueError("parameter grid produced no valid combinations")
    if len(combos) > MAX_SWEEP:
        warnings.append(f"grid truncated {len(combos)}->{MAX_SWEEP} to fit the "
                        f"synchronous request budget")
        combos = combos[:MAX_SWEEP]

    rows = []
    for c in combos:
        r = _run_one(bars, name, c, params)
        rows.append((bt_stats.sharpe(r.returns, per_year), c, r))
    rows.sort(key=lambda x: x[0], reverse=True)

    sharpes = [x[0] for x in rows]
    best_sr, best_cfg, best_r = rows[0]
    cash = float(params.get("cash", 10_000))
    summary = bt_stats.summarize(best_r, per_year, n_trials=len(rows),
                                 cash=cash, trial_sharpes=sharpes)
    naive = bt_stats.summarize(best_r, per_year, n_trials=1, cash=cash)

    cfg = {"strategy": name, "params": best_cfg, "grid": grid,
           "trials": len(rows),
           "fee_bps": params.get("fee_bps", 10),
           "slippage_bps": params.get("slippage_bps", 5)}
    return {
        "ok": True,
        "data": meta,
        "trials": len(rows),
        "best_config": best_cfg,
        "result": summary,
        "uncorrected": {"sharpe": naive["sharpe"],
                        "deflated_sharpe": naive["deflated_sharpe"]},
        "sharpe_spread": {
            "min": round(min(sharpes), 3), "max": round(max(sharpes), 3),
            "median": round(sorted(sharpes)[len(sharpes) // 2], 3),
            "stdev": round(bt_stats._std(sharpes), 3),
        },
        "leaderboard": [{"params": c, "sharpe": round(s, 3)} for s, c, _ in rows[:10]],
        "receipt": bt_data.receipt(meta, cfg, summary),
        "warnings": warnings,
        "note": ("'result.deflated_sharpe' is corrected for all "
                 f"{len(rows)} configurations tested; 'uncorrected' is what a "
                 "backtester that ignores multiple testing would have shown "
                 "you. Prefer the corrected number."),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def api_backtest_strategies(params: dict) -> dict:
    return {
        "ok": True,
        "strategies": {k: {"params": v[1]} for k, v in STRATEGIES.items()},
        "venues": ["coinbase", "kraken", "yahoo"],
        "intervals": sorted(PERIODS),
        "limits": {"max_bars": MAX_BARS, "max_sweep_configs": MAX_SWEEP},
        "notes": [
            "No user code is executed; strategies are a fixed named set.",
            "Signals fill at the next bar's open, never the signal bar's close.",
            "The currently-forming candle is excluded from every series.",
            "Results carry a receipt hashing data + config + result.",
        ],
    }


BACKTEST_ENDPOINTS = {
    "backtest_run": api_backtest_run,
    "backtest_sweep": api_backtest_sweep,
    "backtest_strategies": api_backtest_strategies,
}

BACKTEST_PRICES_USD = {
    "backtest_run": "0.01",
    "backtest_sweep": "0.05",
    "backtest_strategies": "0.001",
}
