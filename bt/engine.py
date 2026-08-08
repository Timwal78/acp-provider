"""Event-driven backtest loop with enforced causality.

Two design choices carry most of the honesty:

1. Look-ahead is made structurally impossible, not merely discouraged. A
   strategy never receives the full price array -- it receives a window that
   raises LookaheadError if it reaches past the current bar. Most backtesters
   hand you the whole series and trust you; that is how a 3.0 Sharpe gets
   built on a strategy that quietly reads tomorrow's close.

2. A signal computed from bar i's close is filled at bar i+1's OPEN, never at
   bar i's close. Filling at the close of the bar that produced the signal is
   the single most common way a backtest invents returns that cannot be traded.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class LookaheadError(RuntimeError):
    """Raised when a strategy tries to read data it could not have known."""


class Series:
    """Read-only view of one field, truncated at the current bar."""

    __slots__ = ("_data", "_limit", "_name")

    def __init__(self, data, name):
        self._data = data
        self._limit = -1
        self._name = name

    def _set_limit(self, i):
        self._limit = i

    def __len__(self):
        return self._limit + 1

    def __getitem__(self, i):
        n = self._limit + 1
        if isinstance(i, slice):
            start, stop, step = i.indices(n)
            return [self._data[j] for j in range(start, stop, step)]
        if i < 0:
            i += n
        if i < 0:
            raise IndexError(f"{self._name}[{i}] before start of series")
        if i > self._limit:
            raise LookaheadError(
                f"strategy read {self._name}[{i}] at bar {self._limit} -- "
                f"that value is in the future"
            )
        return self._data[i]

    def last(self, n=1):
        n = min(n, self._limit + 1)
        return [self._data[j] for j in range(self._limit + 1 - n, self._limit + 1)]


@dataclass
class Window:
    ts: Series
    open: Series
    high: Series
    low: Series
    close: Series
    volume: Series
    i: int = 0
    position: float = 0.0

    def _advance(self, i):
        self.i = i
        for s in (self.ts, self.open, self.high, self.low, self.close, self.volume):
            s._set_limit(i)


@dataclass
class Trade:
    ts: int
    side: str
    price: float
    qty: float
    fee: float


@dataclass
class Result:
    equity: list = field(default_factory=list)
    ts: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    returns: list = field(default_factory=list)
    bars: int = 0
    fees_paid: float = 0.0
    exposure: float = 0.0


def run(bars, strategy, *, cash=10_000.0, fee_bps=10.0, slippage_bps=5.0,
        allow_short=True, rebalance_threshold=0.02):
    """Run one backtest.

    strategy(window) -> target position weight in [-1, 1] of current equity.
    Called once per bar with data visible only up to that bar's close.
    """
    if len(bars) < 3:
        raise ValueError("need at least 3 bars")

    ts = [b[0] for b in bars]
    op = [b[1] for b in bars]
    hi = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    cl = [b[4] for b in bars]
    vo = [b[5] for b in bars]

    w = Window(Series(ts, "ts"), Series(op, "open"), Series(hi, "high"),
               Series(lo, "low"), Series(cl, "close"), Series(vo, "volume"))

    fee_r = fee_bps / 10_000.0
    slip_r = slippage_bps / 10_000.0

    # Cash and position are tracked separately; equity is always derived as
    # cash + position value. Never mutate equity directly -- that is how a
    # backtest ends up double-counting P&L.
    cash_bal = cash
    pos_qty = 0.0          # units held (negative = short)
    res = Result()
    exposed_bars = 0
    prev_equity = None
    pending = 0.0          # target weight decided on the previous bar

    for i in range(len(bars) - 1):
        w._advance(i)
        equity_open = cash_bal + pos_qty * op[i]
        w.position = (pos_qty * op[i]) / equity_open if equity_open > 0 else 0.0

        # Execute the PREVIOUS bar's decision at this bar's open.
        if i > 0 and equity_open > 0:
            fill_px = op[i]
            target_qty = (pending * equity_open) / fill_px if fill_px > 0 else 0.0
            delta = target_qty - pos_qty
            # No-trade band. Holding a target weight means the position drifts
            # as equity moves, so a naive engine emits a tiny rebalance every
            # single bar and bills the strategy for fees it would never pay.
            # Only trade once drift exceeds a share of equity.
            band = rebalance_threshold * equity_open
            if abs(delta * fill_px) > max(band, 1e-9):
                side = "buy" if delta > 0 else "sell"
                # Slippage always works against the trade.
                px = fill_px * (1 + slip_r) if delta > 0 else fill_px * (1 - slip_r)
                notional = abs(delta) * px
                fee = notional * fee_r
                cash_bal -= delta * px      # buying spends cash, selling raises it
                cash_bal -= fee
                res.fees_paid += fee
                pos_qty += delta
                res.trades.append(Trade(ts[i], side, px, abs(delta), fee))

        # Decide for the next bar using only data through bar i.
        target = strategy(w)
        target = 0.0 if target is None else float(target)
        target = max(-1.0, min(1.0, target))
        if not allow_short:
            target = max(0.0, target)
        pending = target

        if pos_qty != 0:
            exposed_bars += 1

        equity = cash_bal + pos_qty * cl[i]   # mark to market on this bar's close
        res.equity.append(equity)
        res.ts.append(ts[i])
        if prev_equity is not None and prev_equity > 0:
            res.returns.append(equity / prev_equity - 1.0)
        prev_equity = equity

    res.bars = len(res.equity)
    res.exposure = exposed_bars / res.bars if res.bars else 0.0
    return res
