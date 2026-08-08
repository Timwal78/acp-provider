#!/usr/bin/env python3
"""Network tests for the data adapters. Kept separate from tests_backtest.py
so CI can run correctness offline and these only when a venue check is wanted.

Every bound asserted here was measured against the live API, not taken from
documentation. Where an exact count is known it is asserted exactly -- an
earlier version of this file asserted ">500 bars" for a 20-day hourly Kraken
pull and reported a failure when the adapter correctly returned exactly 480.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt import feeds

FAILS = []
now = int(time.time())


def chk(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


days = 20
b = feeds.fetch("kraken", "XXBTZUSD", "1h", now - days * 86400, now)
chk("kraken returns exactly one bar per hour", len(b) == days * 24, f"{len(b)} != {days*24}")
chk("kraken respects its 720-bar cap", len(feeds.fetch(
    "kraken", "XXBTZUSD", "1h", now - 60 * 86400, now)) <= 720)

b = feeds.fetch("coinbase", "ETH-USD", "1h", now - 14 * 86400, now)
chk("coinbase paginates past the 300-bar per-request cap", len(b) > 300, f"{len(b)} bars")
chk("coinbase bars are internally consistent",
    all(r[2] >= max(r[1], r[4]) and r[3] <= min(r[1], r[4]) for r in b),
    "high/low do not bracket open/close -- column order may have changed")

b = feeds.fetch("yahoo", "AAPL", "1d", now - 400 * 86400, now)
chk("yahoo daily works", len(b) > 200, f"{len(b)} bars")

for iv, d in (("5m", 90), ("1m", 30), ("1h", 760)):
    try:
        feeds.fetch("yahoo", "SPY", iv, now - d * 86400, now)
        chk(f"yahoo rejects {iv} over {d}d", False, "no error raised")
    except ValueError as e:
        chk(f"yahoo rejects {iv} over {d}d", "capped at" in str(e))

chk("yahoo 5m inside the cap works", len(feeds.fetch(
    "yahoo", "SPY", "5m", now - 55 * 86400, now)) > 2000)

try:
    feeds.fetch("stooq", "spy.us", "1d", now - 365 * 86400, now)
    print("  NOTE  stooq answered from this IP (it does not always)")
except RuntimeError as e:
    chk("stooq failure names the real cause", "blocking this IP" in str(e))

print()
if FAILS:
    print(f"  {len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("  all live adapter tests passed")
