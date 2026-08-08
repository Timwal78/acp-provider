"""Performance statistics, including the ones that say "this is probably noise".

Every backtester reports a Sharpe ratio. A Sharpe ratio computed on the best of
200 tried configurations is not evidence -- it is the maximum of 200 draws, and
its expected value is well above zero even when every strategy is worthless.

The Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014) corrects for exactly
that: it asks whether the observed Sharpe beats what the *best of N random
tries* would have produced anyway, adjusting for skew, kurtosis and sample
length. It is the number that matters, and almost nobody reports it.
"""
from __future__ import annotations

import math
import random

EULER = 0.5772156649015329


def _mean(x):
    return sum(x) / len(x) if x else 0.0


def _std(x, ddof=1):
    n = len(x)
    if n <= ddof:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - ddof))


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p):
    """Inverse normal CDF (Acklam's rational approximation)."""
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def skew(x):
    n = len(x)
    s = _std(x, ddof=1)
    if n < 3 or s == 0:
        return 0.0
    m = _mean(x)
    return (n / ((n - 1) * (n - 2))) * sum(((v - m) / s) ** 3 for v in x)


def kurtosis(x):
    """Non-excess (normal == 3.0), which is what the DSR formula expects."""
    n = len(x)
    s = _std(x, ddof=1)
    if n < 4 or s == 0:
        return 3.0
    m = _mean(x)
    return sum(((v - m) / s) ** 4 for v in x) / n


def sharpe(returns, periods_per_year):
    s = _std(returns)
    if s == 0:
        return 0.0
    return (_mean(returns) / s) * math.sqrt(periods_per_year)


def max_drawdown(equity):
    peak, worst = -float("inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1.0)
    return worst


def deflated_sharpe(returns, periods_per_year, n_trials=1, trial_sharpes=None):
    """Probability the true Sharpe exceeds zero, given n_trials were attempted.

    Below ~0.95 the result should not be treated as a discovery.

    trial_sharpes: the annualized Sharpe of EVERY config tried. The formula
    needs the cross-sectional spread of those Sharpes to know how much a lucky
    maximum is worth. If omitted we fall back to an analytic stand-in, which is
    weaker -- summarize() reports which one was used, because a DSR computed
    from a substituted variance should not be presented as the real thing.
    """
    n = len(returns)
    if n < 10:
        return None
    sr = sharpe(returns, periods_per_year) / math.sqrt(periods_per_year)  # per-period
    g3, g4 = skew(returns), kurtosis(returns)

    # Expected maximum Sharpe from n_trials independent worthless strategies.
    if n_trials > 1:
        if trial_sharpes and len(trial_sharpes) > 1:
            # Observed spread of Sharpes across the configs actually tried,
            # converted to per-period units to match sr.
            v = _std([s / math.sqrt(periods_per_year) for s in trial_sharpes])
        else:
            v = 1.0 / math.sqrt(max(n - 1, 1))
        e_max = v * ((1 - EULER) * _norm_ppf(1 - 1.0 / n_trials)
                     + EULER * _norm_ppf(1 - 1.0 / (n_trials * math.e)))
    else:
        e_max = 0.0

    denom = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if denom <= 0:
        return None
    z = ((sr - e_max) * math.sqrt(n - 1)) / math.sqrt(denom)
    return _norm_cdf(z)


def bootstrap_sharpe_ci(returns, periods_per_year, iters=1000, alpha=0.05, seed=7):
    """Stationary-ish bootstrap CI on the Sharpe ratio."""
    if len(returns) < 20:
        return None
    rng = random.Random(seed)
    n = len(returns)
    out = []
    for _ in range(iters):
        samp = [returns[rng.randrange(n)] for _ in range(n)]
        out.append(sharpe(samp, periods_per_year))
    out.sort()
    lo = out[int(alpha / 2 * iters)]
    hi = out[min(int((1 - alpha / 2) * iters), iters - 1)]
    return round(lo, 3), round(hi, 3)


def summarize(result, periods_per_year, n_trials=1, cash=10_000.0, trial_sharpes=None):
    r = result.returns
    eq = result.equity
    if not r or not eq:
        return {"error": "no returns produced"}
    total = eq[-1] / cash - 1.0
    years = len(r) / periods_per_year if periods_per_year else 0
    cagr = ((eq[-1] / cash) ** (1 / years) - 1.0) if years > 0 and eq[-1] > 0 else None
    sr = sharpe(r, periods_per_year)
    downside = [x for x in r if x < 0]
    dstd = _std(downside) if len(downside) > 1 else 0.0
    sortino = (_mean(r) / dstd * math.sqrt(periods_per_year)) if dstd else None
    mdd = max_drawdown(eq)
    dsr = deflated_sharpe(r, periods_per_year, n_trials, trial_sharpes)
    ci = bootstrap_sharpe_ci(r, periods_per_year)
    var_src = ("observed trial Sharpes" if (trial_sharpes and len(trial_sharpes) > 1)
               else ("analytic approximation" if n_trials > 1 else "n/a (single trial)"))

    verdict = "insufficient data"
    if dsr is not None:
        if dsr >= 0.95:
            verdict = "survives multiple-testing correction"
        elif dsr >= 0.80:
            verdict = "weak — not distinguishable from luck at 95%"
        else:
            verdict = "likely noise / overfit"

    return {
        "bars": result.bars,
        "trades": len(result.trades),
        "exposure": round(result.exposure, 4),
        "total_return": round(total, 4),
        "cagr": round(cagr, 4) if cagr is not None else None,
        "sharpe": round(sr, 3),
        "sortino": round(sortino, 3) if sortino else None,
        "max_drawdown": round(mdd, 4),
        "calmar": round(cagr / abs(mdd), 3) if cagr and mdd else None,
        "fees_paid": round(result.fees_paid, 2),
        "n_trials_declared": n_trials,
        "deflated_sharpe": round(dsr, 4) if dsr is not None else None,
        "dsr_variance_source": var_src,
        "sharpe_95ci": ci,
        "verdict": verdict,
    }
