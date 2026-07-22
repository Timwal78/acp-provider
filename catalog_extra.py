#!/usr/bin/env python3
"""
Extra ownable free-feed endpoints to bulk up the x402 catalog past 60.
No paid vendor APIs. Public sources only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import urllib.error
import urllib.request

UA = "scriptmasterlabs-catalog-extra/1.0 (+https://www.scriptmasterlabs.com)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(url: str, timeout: int = 25) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": str(e)[:240], "url": url}


def _ok(payload: dict) -> dict:
    return {"result": json.dumps(payload, default=str)}


def api_crypto_price(params: dict | None = None) -> dict:
    """Spot price + mcap/vol. Req: { ids?: string csv, vs?: string } default btc,eth,sol"""
    p = params or {}
    ids = (p.get("ids") or p.get("id") or "bitcoin,ethereum,solana").replace(" ", "")
    vs = (p.get("vs") or "usd").lower()
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies={vs}&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
    )
    data = _get(url)
    return _ok({"timestamp": _now(), "vs": vs, "prices": data, "source": "coingecko_simple_price"})


def api_crypto_global(params: dict | None = None) -> dict:
    """Global crypto mcap, BTC dominance, volume."""
    data = _get("https://api.coingecko.com/api/v3/global")
    g = (data or {}).get("data") if isinstance(data, dict) else data
    return _ok({"timestamp": _now(), "global": g, "source": "coingecko_global"})


def api_fx_rate(params: dict | None = None) -> dict:
    """FX rates via Frankfurter (ECB). Req: { base?: string, symbols?: csv }"""
    p = params or {}
    base = (p.get("base") or "USD").upper()
    symbols = (p.get("symbols") or p.get("to") or "").upper().replace(" ", "")
    url = f"https://api.frankfurter.app/latest?from={base}"
    if symbols:
        url += f"&to={symbols}"
    data = _get(url)
    return _ok({"timestamp": _now(), "fx": data, "source": "frankfurter_ecb"})


def api_fear_greed_index(params: dict | None = None) -> dict:
    """Crypto Fear & Greed index."""
    p = params or {}
    limit = p.get("limit") or 1
    try:
        limit = max(1, min(int(limit), 30))
    except Exception:
        limit = 1
    data = _get(f"https://api.alternative.me/fng/?limit={limit}&format=json")
    return _ok({"timestamp": _now(), "fear_greed": data, "source": "alternative_me"})


def api_defi_chains_tvl(params: dict | None = None) -> dict:
    """DefiLlama chains TVL ranking."""
    p = params or {}
    try:
        limit = min(int(p.get("limit") or 25), 100)
    except Exception:
        limit = 25
    data = _get("https://api.llama.fi/v2/chains")
    rows = data if isinstance(data, list) else []
    rows = sorted(rows, key=lambda x: -(x.get("tvl") or 0))[:limit]
    return _ok({
        "timestamp": _now(),
        "count": len(rows),
        "chains": [{"name": r.get("name"), "tvl": r.get("tvl"), "tokenSymbol": r.get("tokenSymbol")} for r in rows],
        "source": "defillama_v2_chains",
    })


def api_defi_protocol_tvl(params: dict | None = None) -> dict:
    """Single protocol TVL. Req: { protocol: string } e.g. aave"""
    p = params or {}
    protocol = (p.get("protocol") or p.get("name") or "aave").strip().lower()
    data = _get(f"https://api.llama.fi/protocol/{protocol}")
    if isinstance(data, dict) and data.get("error"):
        return _ok({"timestamp": _now(), "error": data.get("error"), "protocol": protocol})
    d = data if isinstance(data, dict) else {}
    out = {
        "timestamp": _now(),
        "protocol": protocol,
        "name": d.get("name"),
        "symbol": d.get("symbol"),
        "tvl": d.get("currentChainTvls"),
        "chainTvls": d.get("currentChainTvls"),
        "category": d.get("category"),
        "url": d.get("url"),
        "source": "defillama_protocol",
    }
    data = d
    # current tvl number if present
    if isinstance(data, dict):
        tvl = data.get("tvl")
        if isinstance(tvl, list) and tvl:
            out["latest_tvl_point"] = tvl[-1]
        elif isinstance(tvl, (int, float)):
            out["tvl_usd"] = tvl
        # prefer numeric tvl field if present on root
        if isinstance(data.get("tvl"), (int, float)):
            out["tvl_usd"] = data.get("tvl")
    return _ok(out)


def api_stablecoin_mcap(params: dict | None = None) -> dict:
    """Stablecoin market caps (DefiLlama)."""
    p = params or {}
    try:
        limit = min(int(p.get("limit") or 20), 100)
    except Exception:
        limit = 20
    data = _get("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    pegged = (data or {}).get("peggedAssets") if isinstance(data, dict) else []
    rows = []
    for a in pegged[: limit * 2]:
        circ = (a.get("circulating") or {}).get("peggedUSD") or (a.get("circulation") or {}).get("peggedUSD")
        rows.append({
            "name": a.get("name"),
            "symbol": a.get("symbol"),
            "circulating_usd": circ,
            "price": a.get("price"),
            "chains": a.get("chains"),
        })
    rows = sorted(rows, key=lambda x: -(x.get("circulating_usd") or 0))[:limit]
    return _ok({"timestamp": _now(), "count": len(rows), "stablecoins": rows, "source": "defillama_stablecoins"})


def api_btc_mempool_fees(params: dict | None = None) -> dict:
    """Bitcoin mempool recommended fees (mempool.space)."""
    data = _get("https://mempool.space/api/v1/fees/recommended")
    tip = _get("https://mempool.space/api/blocks/tip/height")
    return _ok({
        "timestamp": _now(),
        "fees_sat_vbyte": data,
        "tip_height": tip,
        "source": "mempool_space",
    })


def api_eth_price(params: dict | None = None) -> dict:
    """ETH spot convenience endpoint."""
    data = _get(
        "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd,btc&include_24hr_change=true&include_market_cap=true"
    )
    return _ok({"timestamp": _now(), "ethereum": data.get("ethereum") if isinstance(data, dict) else data, "source": "coingecko"})


def api_btc_price(params: dict | None = None) -> dict:
    """BTC spot convenience endpoint."""
    data = _get(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd,eur&include_24hr_change=true&include_market_cap=true"
    )
    return _ok({"timestamp": _now(), "bitcoin": data.get("bitcoin") if isinstance(data, dict) else data, "source": "coingecko"})


def api_sol_price(params: dict | None = None) -> dict:
    """SOL spot convenience endpoint."""
    data = _get(
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_24hr_change=true&include_market_cap=true"
    )
    return _ok({"timestamp": _now(), "solana": data.get("solana") if isinstance(data, dict) else data, "source": "coingecko"})


def api_hyperliquid_meta(params: dict | None = None) -> dict:
    """Hyperliquid meta universe (public info endpoint)."""
    # POST style via GET fallback using urllib
    url = "https://api.hyperliquid.xyz/info"
    body = json.dumps({"type": "meta"}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        data = {"error": str(e)[:240]}
    universe = []
    if isinstance(data, dict):
        universe = (data.get("universe") or [])[:50]
    elif isinstance(data, list):
        universe = data[:50]
    return _ok({
        "timestamp": _now(),
        "universe_count": len(data.get("universe") or []) if isinstance(data, dict) else None,
        "universe_sample": universe,
        "source": "hyperliquid_info_meta",
    })


def api_hyperliquid_all_mids(params: dict | None = None) -> dict:
    """Hyperliquid all mid prices."""
    url = "https://api.hyperliquid.xyz/info"
    body = json.dumps({"type": "allMids"}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        data = {"error": str(e)[:240]}
    # optionally filter
    p = params or {}
    coin = (p.get("coin") or p.get("symbol") or "").upper()
    if coin and isinstance(data, dict):
        data = {k: v for k, v in data.items() if coin in k.upper()} if coin else data
    return _ok({"timestamp": _now(), "mids": data, "source": "hyperliquid_all_mids"})


def api_binance_funding(params: dict | None = None) -> dict:
    """Binance USDT-m premium/funding snapshot. Req: { symbol?: string } default BTCUSDT"""
    p = params or {}
    symbol = (p.get("symbol") or "BTCUSDT").upper()
    data = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
    return _ok({"timestamp": _now(), "symbol": symbol, "premium_index": data, "source": "binance_futures_premium_index"})


def api_binance_ticker(params: dict | None = None) -> dict:
    """Binance 24h ticker. Req: { symbol?: string } default BTCUSDT"""
    p = params or {}
    symbol = (p.get("symbol") or "BTCUSDT").upper()
    data = _get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}")
    return _ok({"timestamp": _now(), "symbol": symbol, "ticker": data, "source": "binance_spot_24hr"})


def api_treasury_yields(params: dict | None = None) -> dict:
    """US Treasury daily yield curve (Treasury.gov XML/JSON feed via fiscaldata)."""
    # Fiscal Data API — average interest rates / yield curve approx
    url = (
        "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
        "v2/accounting/od/avg_interest_rates?sort=-record_date&page[size]=20"
    )
    data = _get(url)
    return _ok({"timestamp": _now(), "treasury": data, "source": "fiscaldata_avg_interest_rates"})


def api_openfda_drug_label(params: dict | None = None) -> dict:
    """openFDA drug label search. Req: { q?: string, limit?: int }"""
    p = params or {}
    q = (p.get("q") or p.get("search") or p.get("drug") or "ibuprofen").strip()
    try:
        limit = min(int(p.get("limit") or 5), 20)
    except Exception:
        limit = 5
    from urllib.parse import quote
    url = f"https://api.fda.gov/drug/label.json?search={quote(q)}&limit={limit}"
    data = _get(url)
    return _ok({"timestamp": _now(), "query": q, "openfda": data, "source": "openfda_drug_label"})


def api_clinical_trials_search(params: dict | None = None) -> dict:
    """ClinicalTrials.gov search. Req: { q?: string, page_size?: int }"""
    p = params or {}
    q = (p.get("q") or p.get("query") or "diabetes").strip()
    try:
        page_size = min(int(p.get("page_size") or p.get("limit") or 5), 20)
    except Exception:
        page_size = 5
    from urllib.parse import quote
    url = (
        "https://clinicaltrials.gov/api/v2/studies?"
        f"query.term={quote(q)}&pageSize={page_size}&format=json"
    )
    data = _get(url)
    return _ok({"timestamp": _now(), "query": q, "trials": data, "source": "clinicaltrials_gov_v2"})


def api_sec_company_tickers(params: dict | None = None) -> dict:
    """SEC company tickers map (CIK lookup). Req: { q?: string } filters locally"""
    p = params or {}
    q = (p.get("q") or p.get("ticker") or "").strip().upper()
    data = _get("https://www.sec.gov/files/company_tickers.json")
    rows = []
    if isinstance(data, dict):
        for _, row in list(data.items())[:5000]:
            t = str(row.get("ticker") or "").upper()
            title = str(row.get("title") or "")
            if not q or q in t or q.lower() in title.lower():
                rows.append({"cik": row.get("cik_str"), "ticker": t, "title": title})
            if len(rows) >= 25:
                break
    return _ok({"timestamp": _now(), "query": q or None, "count": len(rows), "companies": rows, "source": "sec_company_tickers"})


def api_coingecko_categories(params: dict | None = None) -> dict:
    """CoinGecko category market data top list."""
    p = params or {}
    try:
        limit = min(int(p.get("limit") or 25), 100)
    except Exception:
        limit = 25
    data = _get("https://api.coingecko.com/api/v3/coins/categories")
    rows = data if isinstance(data, list) else []
    out = []
    for r in rows[:limit]:
        out.append({
            "id": r.get("id") or r.get("category_id"),
            "name": r.get("name"),
            "market_cap": r.get("market_cap"),
            "market_cap_change_24h": r.get("market_cap_change_24h"),
            "top_3_coins": r.get("top_3_coins"),
        })
    return _ok({"timestamp": _now(), "count": len(out), "categories": out, "source": "coingecko_categories"})


def api_defi_yields_pools(params: dict | None = None) -> dict:
    """DefiLlama yields pools top APY. Req: { limit?: int, chain?: string, stable?: bool }"""
    p = params or {}
    try:
        limit = min(int(p.get("limit") or 20), 50)
    except Exception:
        limit = 20
    chain = (p.get("chain") or "").lower()
    stable = str(p.get("stable") or "").lower() in ("1", "true", "yes")
    data = _get("https://yields.llama.fi/pools")
    pools = (data or {}).get("data") if isinstance(data, dict) else []
    rows = []
    for pool in pools or []:
        if chain and chain not in str(pool.get("chain") or "").lower():
            continue
        if stable and not pool.get("stablecoin"):
            continue
        rows.append({
            "pool": pool.get("pool"),
            "project": pool.get("project"),
            "symbol": pool.get("symbol"),
            "chain": pool.get("chain"),
            "apy": pool.get("apy"),
            "tvlUsd": pool.get("tvlUsd"),
            "stablecoin": pool.get("stablecoin"),
        })
        if len(rows) >= limit * 3:
            break
    rows = sorted(rows, key=lambda x: -(x.get("apy") or 0))[:limit]
    return _ok({"timestamp": _now(), "count": len(rows), "pools": rows, "source": "defillama_yields_pools"})


def api_l2_activity(params: dict | None = None) -> dict:
    """L2Beat-like summary via DefiLlama chains filtered to L2 names heuristic."""
    data = _get("https://api.llama.fi/v2/chains")
    rows = data if isinstance(data, list) else []
    l2_keys = ("base", "arbitrum", "optimism", "polygon", "zksync", "scroll", "linea", "mantle", "blast", "mode", "ink")
    out = []
    for r in rows:
        name = str(r.get("name") or "")
        if any(k in name.lower() for k in l2_keys):
            out.append({"name": name, "tvl": r.get("tvl"), "tokenSymbol": r.get("tokenSymbol")})
    out = sorted(out, key=lambda x: -(x.get("tvl") or 0))[:30]
    return _ok({"timestamp": _now(), "count": len(out), "l2s": out, "source": "defillama_chains_l2_filter"})


def api_geckoterminal_trending(params: dict | None = None) -> dict:
    """GeckoTerminal trending pools (public)."""
    data = _get("https://api.geckoterminal.com/api/v2/networks/trending_pools?page=1")
    pools = []
    if isinstance(data, dict):
        for item in (data.get("data") or [])[:20]:
            attr = item.get("attributes") or {}
            pools.append({
                "name": attr.get("name"),
                "address": attr.get("address"),
                "network": (item.get("relationships") or {}).get("network", {}).get("data", {}).get("id"),
                "price_change_percentage": attr.get("price_change_percentage"),
                "volume_usd": attr.get("volume_usd"),
                "reserve_in_usd": attr.get("reserve_in_usd"),
            })
    return _ok({"timestamp": _now(), "count": len(pools), "trending_pools": pools, "source": "geckoterminal_trending_pools"})


def api_dexscreener_boosts(params: dict | None = None) -> dict:
    """DexScreener latest token boosts."""
    data = _get("https://api.dexscreener.com/token-boosts/latest/v1")
    rows = data if isinstance(data, list) else []
    return _ok({"timestamp": _now(), "count": len(rows[:30]), "boosts": rows[:30], "source": "dexscreener_token_boosts"})


def api_dexscreener_search(params: dict | None = None) -> dict:
    """DexScreener token search. Req: { q: string }"""
    p = params or {}
    q = (p.get("q") or p.get("query") or "eth").strip()
    from urllib.parse import quote
    data = _get(f"https://api.dexscreener.com/latest/dex/search?q={quote(q)}")
    pairs = (data or {}).get("pairs") if isinstance(data, dict) else []
    out = []
    for pair in (pairs or [])[:15]:
        out.append({
            "chainId": pair.get("chainId"),
            "dexId": pair.get("dexId"),
            "pairAddress": pair.get("pairAddress"),
            "baseToken": pair.get("baseToken"),
            "priceUsd": pair.get("priceUsd"),
            "liquidity": pair.get("liquidity"),
            "volume": pair.get("volume"),
            "url": pair.get("url"),
        })
    return _ok({"timestamp": _now(), "query": q, "count": len(out), "pairs": out, "source": "dexscreener_search"})


# Registry consumed by provider.py
EXTRA_ENDPOINTS = {
    "crypto_price": api_crypto_price,
    "crypto_global": api_crypto_global,
    "fx_rate": api_fx_rate,
    "fear_greed_index": api_fear_greed_index,
    "defi_chains_tvl": api_defi_chains_tvl,
    "defi_protocol_tvl": api_defi_protocol_tvl,
    "stablecoin_mcap": api_stablecoin_mcap,
    "btc_mempool_fees": api_btc_mempool_fees,
    "eth_price": api_eth_price,
    "btc_price": api_btc_price,
    "sol_price": api_sol_price,
    "hyperliquid_meta": api_hyperliquid_meta,
    "hyperliquid_all_mids": api_hyperliquid_all_mids,
    "binance_funding": api_binance_funding,
    "binance_ticker": api_binance_ticker,
    "treasury_yields": api_treasury_yields,
    "openfda_drug_label": api_openfda_drug_label,
    "clinical_trials_search": api_clinical_trials_search,
    "sec_company_tickers": api_sec_company_tickers,
    "coingecko_categories": api_coingecko_categories,
    "defi_yields_pools": api_defi_yields_pools,
    "l2_activity": api_l2_activity,
    "geckoterminal_trending": api_geckoterminal_trending,
    "dexscreener_boosts": api_dexscreener_boosts,
    "dexscreener_search": api_dexscreener_search,
}

EXTRA_PRICES_USD = {
    "crypto_price": "0.10",
    "crypto_global": "0.10",
    "fx_rate": "0.08",
    "fear_greed_index": "0.08",
    "defi_chains_tvl": "0.15",
    "defi_protocol_tvl": "0.15",
    "stablecoin_mcap": "0.15",
    "btc_mempool_fees": "0.08",
    "eth_price": "0.05",
    "btc_price": "0.05",
    "sol_price": "0.05",
    "hyperliquid_meta": "0.15",
    "hyperliquid_all_mids": "0.15",
    "binance_funding": "0.12",
    "binance_ticker": "0.10",
    "treasury_yields": "0.12",
    "openfda_drug_label": "0.12",
    "clinical_trials_search": "0.12",
    "sec_company_tickers": "0.10",
    "coingecko_categories": "0.12",
    "defi_yields_pools": "0.18",
    "l2_activity": "0.12",
    "geckoterminal_trending": "0.12",
    "dexscreener_boosts": "0.12",
    "dexscreener_search": "0.12",
}

EXTRA_ACP_DEFAULTS = {k: 0.02 for k in EXTRA_ENDPOINTS}
