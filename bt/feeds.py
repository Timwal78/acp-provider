"""Market data adapters, restricted to venues reachable from production.

Measured from the Render egress IP via the service's own web-fetch, not from a
dev box (the two differ -- CoinGecko answers a laptop fine while rate-limiting
the server):

    reachable : Coinbase, Kraken, OKX, Binance.US, Yahoo
    blocked   : Binance.com (451), Bybit (403), CoinGecko (429)
    unreliable: Stooq -- answered the earlier production-IP probe but serves a
                796-byte HTML block for every symbol from other IPs, so it is
                a fallback at best and must never be a sole source

So Binance and CoinGecko are deliberately absent. Every adapter returns the
same shape: a list of (ts, open, high, low, close, volume) sorted ascending,
deduplicated, with no synthesized bars -- gaps stay gaps, because filling them
silently turns a data outage into a fake flat price.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; ScriptMasterLabs-backtest/1.0)"}

COINBASE_GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
MAX_BARS_PER_REQ = 300  # Coinbase caps a ranged request at 300 candles

# Yahoo silently 422s past an undocumented per-interval lookback. These bounds
# were measured against SPY, not read off a doc page: 58d succeeds and 60d
# returns HTTP 422 for 5m/15m/30m; 700d succeeds and 730d fails for 1h; 7d
# succeeds and 30d fails for 1m. Requests beyond these are rejected up front
# with a useful message instead of surfacing a bare 422.
YAHOO_MAX_DAYS = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 729, "1d": None}


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str, timeout: int = 30):
    return json.loads(_get(url, timeout))


def _clean(bars):
    """Sort ascending, drop duplicate timestamps, drop malformed rows."""
    seen, out = set(), []
    for b in sorted(bars, key=lambda x: x[0]):
        ts = int(b[0])
        if ts in seen:
            continue
        try:
            row = (ts, float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
        except (TypeError, ValueError):
            continue
        if row[2] < row[3]:  # high < low means the row is garbage
            continue
        seen.add(ts)
        out.append(row)
    return out


def coinbase(symbol: str, interval: str, start: int, end: int, pace: float = 0.20):
    """Paginated Coinbase candles. Verified to reach 2018 hourly data.

    Coinbase returns [time, low, high, open, close, volume] -- note that order,
    it is NOT OHLC, and reading it positionally as OHLC silently corrupts every
    high and low in the series.
    """
    gran = COINBASE_GRAN.get(interval)
    if not gran:
        raise ValueError(f"coinbase: unsupported interval {interval}")
    span = gran * MAX_BARS_PER_REQ
    out, cur = [], start
    while cur < end:
        stop = min(cur + span, end)
        url = (f"https://api.exchange.coinbase.com/products/{symbol}/candles"
               f"?granularity={gran}"
               f"&start={time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(cur))}Z"
               f"&end={time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(stop))}Z")
        try:
            rows = _get_json(url)
        except Exception:
            time.sleep(1.0)
            cur = stop
            continue
        for r in rows or []:
            # r = [time, low, high, open, close, volume]
            out.append((r[0], r[3], r[2], r[1], r[4], r[5]))
        cur = stop
        # Measured: a 12-request burst was clean at 0.05s (7.9 req/s), so this
        # is deliberately ~2x slower. A short burst is NOT proof that a 150-
        # request backfill is safe, and this server is already 451'd by Binance
        # and 429'd by CoinGecko -- losing Coinbase too would be expensive.
        time.sleep(pace)
    return _clean(out)


def kraken(pair: str, interval: str, start: int, end: int):
    """Kraken OHLC. Measured cap is 721 bars, not 720: 720 closed candles plus
    the one currently forming. Fine for recent windows, not a deep-history
    source -- a 120-day hourly request still comes back spanning 30 days."""
    mins = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "6h": 360, "1d": 1440}.get(interval)
    if not mins:
        raise ValueError(f"kraken: unsupported interval {interval}")
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={mins}&since={start}"
    d = _get_json(url)
    if d.get("error"):
        raise RuntimeError(f"kraken: {d['error']}")
    series = next(v for k, v in d["result"].items() if k != "last")
    rows = [(r[0], r[1], r[2], r[3], r[4], r[6]) for r in series if start <= int(r[0]) <= end]
    return _clean(rows)


def yahoo(symbol: str, interval: str, start: int, end: int):
    """Yahoo chart -- the equities path. Daily reaches decades; intraday is
    hard-capped per interval (see YAHOO_MAX_DAYS, measured not assumed)."""
    iv = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
          "1h": "1h", "1d": "1d"}.get(interval)
    if not iv:
        raise ValueError(f"yahoo: unsupported interval {interval}")
    cap = YAHOO_MAX_DAYS.get(interval)
    span_days = (end - start) / 86400.0
    if cap is not None and span_days > cap:
        raise ValueError(
            f"yahoo: {interval} lookback is capped at {cap} days (measured); "
            f"asked for {span_days:.0f}. Use a shorter window, a coarser "
            f"interval, or coinbase/kraken for crypto.")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval={iv}&period1={start}&period2={end}")
    d = _get_json(url)
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError(f"yahoo: empty result for {symbol}")
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote") or [{}]
    q = q[0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        v = q.get("volume")
        vals = [o[i] if o else None, h[i] if h else None,
                l[i] if l else None, c[i] if c else None, (v[i] if v else 0) or 0]
        if any(x is None for x in vals[:4]):
            continue  # Yahoo emits nulls on halts; skip, never forward-fill
        rows.append((t, vals[0], vals[1], vals[2], vals[3], vals[4]))
    return _clean(rows)


def stooq(symbol: str, start: int, end: int):
    """Stooq daily CSV -- deep free daily equities history, no key."""
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i=d"
    text = _get(url).decode("utf-8", "replace")
    if "<html" in text[:200].lower():
        # Measured: stooq returns an identical 796-byte HTML page for EVERY
        # symbol from some IPs, including valid ones. That is a block, not a
        # bad ticker, and saying "unknown symbol" sends you debugging the
        # wrong thing. Treat stooq as unavailable and fail over to yahoo.
        raise RuntimeError(
            "stooq: served HTML instead of CSV -- source is blocking this IP, "
            "not a symbol error; use yahoo for equities")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            ts = int(time.mktime(time.strptime(row["Date"], "%Y-%m-%d")))
        except Exception:
            continue
        if not (start <= ts <= end):
            continue
        rows.append((ts, row["Open"], row["High"], row["Low"], row["Close"],
                     row.get("Volume") or 0))
    return _clean(rows)


VENUES = {"coinbase": coinbase, "kraken": kraken, "yahoo": yahoo, "stooq": stooq}

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                    "1h": 3600, "6h": 21600, "1d": 86400}


def fetch(venue: str, symbol: str, interval: str, start: int, end: int,
          drop_forming: bool = True):
    """Fetch bars, dropping the candle that has not finished yet.

    Every venue happily returns the bar currently in progress when the range
    ends at now -- Kraken's cap is 721 for exactly this reason (720 closed + 1
    forming). That bar's high, low and close all still move. Backtesting over
    it means the last data point changes between runs, which breaks
    reproducibility and lets a strategy act on a close that had not happened.
    """
    fn = VENUES.get(venue)
    if not fn:
        raise ValueError(f"unknown venue {venue}; have {sorted(VENUES)}")
    bars = fn(symbol, start, end) if venue == "stooq" else fn(symbol, interval, start, end)
    if drop_forming and bars:
        step = INTERVAL_SECONDS.get(interval)
        if step:
            cutoff = time.time()
            bars = [b for b in bars if b[0] + step <= cutoff]
    return bars
