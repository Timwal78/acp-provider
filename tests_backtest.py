#!/usr/bin/env python3
"""Correctness tests. These are the claims the product rests on."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt import engine, stats

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def synth(n=300, drift=0.001, seed=3):
    import random
    rng = random.Random(seed)
    px, bars, t = 100.0, [], 1_600_000_000
    for i in range(n):
        o = px
        px *= math.exp(drift + rng.gauss(0, 0.01))
        c = px
        bars.append((t + i * 3600, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 10.0))
    return bars


bars = synth()

# 1. A cheating strategy must be caught, not quietly rewarded.
def cheater(w):
    return 1.0 if w.close[w.i + 1] > w.close[w.i] else -1.0   # reads the future

caught = False
try:
    engine.run(bars, cheater)
except engine.LookaheadError as e:
    caught = True
    msg = str(e)
check("look-ahead is blocked", caught, "cheating strategy ran without error")
if caught:
    check("error names the offending read", "future" in msg.lower())

# 2. Buy-and-hold must match the underlying move, minus one entry cost.
def buyhold(w):
    return 1.0

r = engine.run(bars, buyhold, cash=10_000, fee_bps=0, slippage_bps=0)
underlying = bars[-2][4] / bars[1][1] - 1.0
got = r.equity[-1] / 10_000 - 1.0
check("buy & hold tracks underlying", abs(got - underlying) < 0.02,
      f"got {got:.4f} vs underlying {underlying:.4f}")
check("buy & hold trades once", len(r.trades) == 1, f"{len(r.trades)} trades")

# 3. Flat strategy must be exactly flat -- no drift, no phantom P&L.
flat = engine.run(bars, lambda w: 0.0, cash=10_000, fee_bps=10)
check("flat strategy stays at cash", abs(flat.equity[-1] - 10_000) < 1e-6,
      f"ended at {flat.equity[-1]:.6f}")
check("flat strategy pays no fees", flat.fees_paid == 0)

# 4. Costs must reduce returns, monotonically.
cheap = engine.run(bars, lambda w: 1.0 if w.i % 4 < 2 else -1.0, fee_bps=0, slippage_bps=0)
dear = engine.run(bars, lambda w: 1.0 if w.i % 4 < 2 else -1.0, fee_bps=50, slippage_bps=25)
check("higher costs lower equity", dear.equity[-1] < cheap.equity[-1],
      f"{dear.equity[-1]:.2f} vs {cheap.equity[-1]:.2f}")
check("fees are accumulated", dear.fees_paid > 0)

# 5. Fills happen at the NEXT open, never the signal bar's close.
seen = {}
def once(w):
    if w.i == 5:
        seen["signal_bar"] = w.i
        return 1.0
    return 1.0 if w.i > 5 else 0.0

r5 = engine.run(bars, once, fee_bps=0, slippage_bps=0)
first = r5.trades[0]
check("fill is at next bar's open", abs(first.price - bars[6][1]) < 1e-9,
      f"filled {first.price:.4f}, bar6 open {bars[6][1]:.4f}, bar5 close {bars[5][4]:.4f}")

# 6. Slippage must always hurt, on both sides.
b = engine.run(bars, lambda w: 1.0, fee_bps=0, slippage_bps=100)
check("buy slips upward", b.trades[0].price > bars[1][1])
s = engine.run(bars, lambda w: -1.0, fee_bps=0, slippage_bps=100)
check("sell slips downward", s.trades[0].price < bars[1][1])

# 7. Deflated Sharpe must punish multiple testing.
rets = [0.001 + 0.01 * math.sin(i) for i in range(500)]
d1 = stats.deflated_sharpe(rets, 8760, n_trials=1)
d200 = stats.deflated_sharpe(rets, 8760, n_trials=200)
check("more trials lowers deflated Sharpe", d200 < d1, f"1 trial {d1:.4f}, 200 trials {d200:.4f}")

# 8. Max drawdown sign and magnitude.
check("drawdown is negative", stats.max_drawdown([100, 120, 60, 80]) < 0)
check("drawdown magnitude correct", abs(stats.max_drawdown([100, 120, 60, 80]) - (-0.5)) < 1e-9)

print()
if FAILS:
    print(f"  {len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("  all correctness tests passed")
