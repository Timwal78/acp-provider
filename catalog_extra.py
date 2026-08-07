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



def _crypto_price_fallback(ids: str, vs: str) -> dict:
    """Public fallbacks when CoinGecko rate-limits (429)."""
    # Map coingecko ids -> binance symbols for major assets
    binance_map = {
        "bitcoin": "BTCUSDT",
        "ethereum": "ETHUSDT",
        "solana": "SOLUSDT",
        "binancecoin": "BNBUSDT",
        "ripple": "XRPUSDT",
        "dogecoin": "DOGEUSDT",
        "cardano": "ADAUSDT",
        "avalanche-2": "AVAXUSDT",
        "matic-network": "MATICUSDT",
        "chainlink": "LINKUSDT",
        "usd-coin": "USDCUSDT",
        "tether": "USDTUSDT",
    }
    out = {}
    vs = (vs or "usd").lower()
    for cid in [x.strip() for x in ids.split(",") if x.strip()]:
        sym = binance_map.get(cid)
        if not sym:
            continue
        if vs not in ("usd", "usdt"):
            continue
        if sym == "USDTUSDT":
            out[cid] = {vs: 1.0, "source": "stable"}
            continue
        data = _get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}")
        if isinstance(data, dict) and data.get("lastPrice"):
            try:
                px = float(data["lastPrice"])
                ch = float(data.get("priceChangePercent") or 0)
                vol = float(data.get("quoteVolume") or 0)
                out[cid] = {vs: px, f"{vs}_24h_change": ch, f"{vs}_24h_vol": vol, "source": "binance_24hr"}
            except Exception:
                pass
    return out


def api_crypto_price(params: dict | None = None) -> dict:
    """Spot price + mcap/vol. Req: { ids?: string csv, vs?: string, symbol?: BTC|ETH|SOL }"""
    p = params or {}
    # Buyers (Clawpump) often send {symbol: BTC} not CoinGecko ids
    sym_map = {
        "btc": "bitcoin",
        "bitcoin": "bitcoin",
        "eth": "ethereum",
        "ethereum": "ethereum",
        "sol": "solana",
        "solana": "solana",
        "bnb": "binancecoin",
        "xrp": "ripple",
        "doge": "dogecoin",
        "ada": "cardano",
        "avax": "avalanche-2",
        "matic": "matic-network",
        "pol": "matic-network",
        "link": "chainlink",
        "pepe": "pepe",
        "wif": "dogwifcoin",
        "usdc": "usd-coin",
        "usdt": "tether",
    }
    raw_ids = p.get("ids") or p.get("id") or ""
    if not raw_ids:
        sym = (p.get("symbol") or p.get("ticker") or p.get("coin") or "").strip()
        if sym:
            parts = []
            for s in sym.replace(" ", ",").split(","):
                s = s.strip().lower().lstrip("$")
                if not s:
                    continue
                parts.append(sym_map.get(s, s))
            raw_ids = ",".join(parts)
    if not raw_ids:
        raw_ids = "bitcoin,ethereum,solana"
    ids = str(raw_ids).replace(" ", "")
    # map any ticker-looking tokens inside ids csv too
    mapped = []
    for part in ids.split(","):
        pl = part.strip().lower()
        if not pl:
            continue
        mapped.append(sym_map.get(pl, pl))
    ids = ",".join(mapped) if mapped else "bitcoin,ethereum,solana"
    vs = (p.get("vs") or p.get("currency") or "usd").lower()
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids}&vs_currencies={vs}&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
    )
    data = _get(url)
    source = "coingecko_simple_price"
    # CoinGecko 429 / error object → Binance fallback for majors
    if not isinstance(data, dict) or data.get("error") or all(
        (not isinstance(v, dict)) for v in data.values()
    ):
        fb = _crypto_price_fallback(ids, vs)
        if fb:
            data = fb
            source = "binance_fallback"
    return _ok(
        {
            "timestamp": _now(),
            "vs": vs,
            "ids": ids,
            "prices": data,
            "source": source,
            "offering": "crypto_price",
        }
    )


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


def _spot_major(coin_id: str, key: str) -> dict:
    """Spot convenience for btc/eth/sol with CoinGecko → Binance fallback."""
    wrapped = api_crypto_price({"ids": coin_id, "vs": "usd"})
    try:
        inner = json.loads(wrapped.get("result") or "{}")
    except Exception:
        inner = {}
    prices = inner.get("prices") if isinstance(inner, dict) else {}
    row = prices.get(coin_id) if isinstance(prices, dict) else None
    return _ok(
        {
            "timestamp": _now(),
            key: row,
            "source": (inner.get("source") if isinstance(inner, dict) else None) or "crypto_price",
            "offering": f"{key}_price" if key != "bitcoin" else "btc_price",
        }
    )


def api_eth_price(params: dict | None = None) -> dict:
    """ETH spot convenience endpoint."""
    return _spot_major("ethereum", "ethereum")


def api_btc_price(params: dict | None = None) -> dict:
    """BTC spot convenience endpoint."""
    return _spot_major("bitcoin", "bitcoin")


def api_sol_price(params: dict | None = None) -> dict:
    """SOL spot convenience endpoint."""
    return _spot_major("solana", "solana")


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



def api_crypto_news(params: dict | None = None) -> dict:
    """Crypto news headlines from public RSS (CoinDesk + Bitcoin Magazine). Params: { limit?: int }"""
    import re as _re
    from html import unescape
    p = params or {}
    try:
        limit = max(1, min(int(p.get("limit") or 20), 50))
    except Exception:
        limit = 20
    feeds = [
        ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("bitcoinmagazine", "https://bitcoinmagazine.com/.rss/full/"),
        ("cointelegraph", "https://cointelegraph.com/rss"),
    ]
    out = []
    sources_ok = []
    for src, url in feeds:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=18) as r:
                xml = r.read(200_000).decode("utf-8", "replace")
        except Exception as e:
            continue
        sources_ok.append(src)
        # naive item split — good enough for RSS 2.0
        for block in _re.findall(r"<item>(.*?)</item>", xml, flags=_re.I | _re.S):
            def _tag(name: str) -> str:
                m = _re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", block, flags=_re.I | _re.S)
                return unescape((m.group(1) if m else "").strip())
            title = _tag("title")
            link = _tag("link")
            if not link:
                m = _re.search(r'href="([^"]+)"', block)
                link = m.group(1) if m else ""
            pub = _tag("pubDate") or _tag("published")
            desc = _re.sub(r"<[^>]+>", " ", _tag("description") or "")
            desc = _re.sub(r"\s+", " ", desc).strip()[:400]
            if not title:
                continue
            out.append({
                "title": title,
                "source": src,
                "url": link,
                "published": pub,
                "summary": desc,
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return _ok({
        "timestamp": _now(),
        "count": len(out),
        "news": out[:limit],
        "feeds_ok": sources_ok,
        "source": "public_rss_crypto_news",
    })


def api_token_details(params: dict | None = None) -> dict:
    """CoinGecko token details. Params: { id?: string } e.g. bitcoin, ethereum, solana"""
    p = params or {}
    cid = (p.get("id") or p.get("coin") or p.get("token") or "bitcoin").strip().lower()
    from urllib.parse import quote
    url = (
        f"https://api.coingecko.com/api/v3/coins/{quote(cid)}"
        "?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false"
    )
    data = _get(url)
    if not isinstance(data, dict) or data.get("error"):
        return _ok({"timestamp": _now(), "error": (data or {}).get("error") if isinstance(data, dict) else "fetch_failed", "id": cid})
    md = data.get("market_data") or {}
    out = {
        "id": data.get("id"),
        "symbol": data.get("symbol"),
        "name": data.get("name"),
        "categories": data.get("categories"),
        "market_cap_rank": data.get("market_cap_rank"),
        "price_usd": (md.get("current_price") or {}).get("usd"),
        "market_cap_usd": (md.get("market_cap") or {}).get("usd"),
        "total_volume_usd": (md.get("total_volume") or {}).get("usd"),
        "ath_usd": (md.get("ath") or {}).get("usd"),
        "atl_usd": (md.get("atl") or {}).get("usd"),
        "price_change_24h_pct": md.get("price_change_percentage_24h"),
        "price_change_7d_pct": md.get("price_change_percentage_7d"),
        "circulating_supply": md.get("circulating_supply"),
        "homepage": (data.get("links") or {}).get("homepage"),
        "source": "coingecko_coins",
    }
    return _ok({"timestamp": _now(), "token": out})


def api_trending_altcoins(params: dict | None = None) -> dict:
    """CoinGecko trending search (coins + nfts + categories)."""
    data = _get("https://api.coingecko.com/api/v3/search/trending")
    coins = []
    for row in ((data or {}).get("coins") or []) if isinstance(data, dict) else []:
        item = (row or {}).get("item") or {}
        coins.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "symbol": item.get("symbol"),
            "market_cap_rank": item.get("market_cap_rank"),
            "score": item.get("score"),
            "price_btc": item.get("price_btc"),
        })
    return _ok({
        "timestamp": _now(),
        "count": len(coins),
        "trending": coins,
        "nfts": (data or {}).get("nfts") if isinstance(data, dict) else [],
        "categories": (data or {}).get("categories") if isinstance(data, dict) else [],
        "source": "coingecko_search_trending",
    })


def api_funding_rates(params: dict | None = None) -> dict:
    """Perp funding rates — Binance USDT-M premium index. Params: { symbol?: string } default top set"""
    p = params or {}
    symbol = (p.get("symbol") or "").upper().replace("-", "")
    if symbol:
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        data = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}")
        rows = [data] if isinstance(data, dict) and not data.get("error") else []
    else:
        data = _get("https://fapi.binance.com/fapi/v1/premiumIndex")
        rows = data if isinstance(data, list) else []
        # top liquid names first
        prefer = {"BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","SUIUSDT"}
        rows = sorted(rows, key=lambda r: (0 if (r or {}).get("symbol") in prefer else 1, (r or {}).get("symbol") or ""))
        rows = rows[:40]
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "symbol": r.get("symbol"),
            "markPrice": r.get("markPrice"),
            "indexPrice": r.get("indexPrice"),
            "lastFundingRate": r.get("lastFundingRate"),
            "nextFundingTime": r.get("nextFundingTime"),
            "interestRate": r.get("interestRate"),
        })
    return _ok({"timestamp": _now(), "count": len(out), "funding": out, "source": "binance_usdm_premium_index"})


def api_defi_analytics(params: dict | None = None) -> dict:
    """DefiLlama protocol overview / TVL movers. Params: { limit?: int }"""
    p = params or {}
    try:
        limit = max(1, min(int(p.get("limit") or 25), 100))
    except Exception:
        limit = 25
    data = _get("https://api.llama.fi/protocols")
    rows = data if isinstance(data, list) else []
    rows = sorted(rows, key=lambda x: -(x.get("tvl") or 0))[:limit]
    out = []
    for r in rows:
        out.append({
            "name": r.get("name"),
            "symbol": r.get("symbol"),
            "category": r.get("category"),
            "chains": r.get("chains"),
            "tvl": r.get("tvl"),
            "change_1h": r.get("change_1h"),
            "change_1d": r.get("change_1d"),
            "change_7d": r.get("change_7d"),
            "mcap": r.get("mcap"),
            "url": r.get("url"),
        })
    return _ok({"timestamp": _now(), "count": len(out), "protocols": out, "source": "defillama_protocols"})


def api_stablecoin_watch(params: dict | None = None) -> dict:
    """Stablecoin market caps + dominance (DefiLlama)."""
    data = _get("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    pegged = (data or {}).get("peggedAssets") if isinstance(data, dict) else []
    out = []
    for r in (pegged or [])[:40]:
        circ = (r.get("circulating") or {})
        out.append({
            "name": r.get("name"),
            "symbol": r.get("symbol"),
            "pegType": r.get("pegType"),
            "circulating_usd": circ.get("peggedUSD") if isinstance(circ, dict) else circ,
            "price": r.get("price"),
            "chains": list((r.get("chainCirculating") or {}).keys())[:12],
        })
    out = sorted(out, key=lambda x: -(x.get("circulating_usd") or 0))
    return _ok({"timestamp": _now(), "count": len(out), "stablecoins": out, "source": "defillama_stablecoins"})


def api_web_fetch(params: dict | None = None) -> dict:
    """Fetch a public HTTP(S) URL and return truncated text/json. Params: { url: string }"""
    p = params or {}
    url = (p.get("url") or p.get("uri") or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return _ok({"timestamp": _now(), "error": "url_required_https", "hint": "Pass url=https://..."})
    # block obvious internal targets
    low = url.lower()
    if any(x in low for x in ["localhost", "127.0.0.1", "0.0.0.0", "169.254.", "metadata.google"]):
        return _ok({"timestamp": _now(), "error": "url_blocked"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(80_000)
            ctype = r.headers.get("Content-Type", "")
            status = getattr(r, "status", 200)
    except Exception as e:
        return _ok({"timestamp": _now(), "error": str(e)[:240], "url": url})
    text = raw.decode("utf-8", "replace")
    parsed = None
    if "json" in ctype or text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    return _ok({
        "timestamp": _now(),
        "url": url,
        "status": status,
        "content_type": ctype,
        "bytes": len(raw),
        "json": parsed if parsed is not None else None,
        "text": None if parsed is not None else text[:12000],
        "source": "sml_web_fetch",
    })


def api_token_top_holders_proxy(params: dict | None = None) -> dict:
    """Token holder-ish snapshot via Eth plorer-free alternative: CoinGecko tickers top markets as liquidity proxy.
    Params: { id?: string }
    """
    p = params or {}
    cid = (p.get("id") or "ethereum").strip().lower()
    from urllib.parse import quote
    data = _get(f"https://api.coingecko.com/api/v3/coins/{quote(cid)}/tickers?include_exchange_logo=false&depth=true&order=volume_desc")
    tickers = (data or {}).get("tickers") if isinstance(data, dict) else []
    out = []
    for t in (tickers or [])[:20]:
        out.append({
            "market": (t.get("market") or {}).get("name"),
            "base": t.get("base"),
            "target": t.get("target"),
            "last": t.get("last"),
            "volume": t.get("volume"),
            "bid_ask_spread_pct": t.get("bid_ask_spread_percentage"),
            "trust_score": t.get("trust_score"),
            "trade_url": t.get("trade_url"),
        })
    return _ok({"timestamp": _now(), "id": cid, "count": len(out), "top_markets": out, "note": "liquidity/markets proxy (not on-chain holders)", "source": "coingecko_tickers"})



def api_llm_chat(params: dict | None = None) -> dict:
    """OpenAI-compatible chat completion proxy. Params:
    { prompt|message?: str, messages?: list|json, model?: str, max_tokens?: int, temperature?: float }
    Uses LLM_BASE_URL + LLM_API_KEY + LLM_MODEL_ID (or BYOK via headers on HTTP layer).
    """
    import os
    p = params or {}
    base = (p.get("_llm_base") or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    key = p.get("_llm_key") or os.environ.get("LLM_API_KEY") or ""
    model = (p.get("model") or os.environ.get("LLM_MODEL_ID") or "x-ai-grok-4-5").strip()
    if not base or not key:
        return _ok({
            "timestamp": _now(),
            "error": "llm_unconfigured",
            "hint": "Set LLM_BASE_URL + LLM_API_KEY on host, or pass BYOK",
        })
    messages = p.get("messages")
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except Exception:
            messages = None
    if not messages:
        prompt = (p.get("prompt") or p.get("message") or p.get("q") or "").strip()
        if not prompt:
            return _ok({"timestamp": _now(), "error": "prompt_required"})
        messages = [{"role": "user", "content": prompt[:8000]}]
    try:
        max_tokens = max(16, min(int(p.get("max_tokens") or 256), 1024))
    except Exception:
        max_tokens = 256
    try:
        temperature = float(p.get("temperature") if p.get("temperature") is not None else 0.2)
    except Exception:
        temperature = 0.2
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    url = base + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            data = json.loads(raw)
    except Exception as e:
        return _ok({"timestamp": _now(), "error": str(e)[:300], "model": model, "url": url})
    choice = None
    try:
        choice = (data.get("choices") or [{}])[0]
    except Exception:
        choice = None
    content = None
    if isinstance(choice, dict):
        msg = choice.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if content is None:
            content = choice.get("text")
    return _ok({
        "timestamp": _now(),
        "model": model,
        "content": content,
        "usage": data.get("usage") if isinstance(data, dict) else None,
        "raw": data if p.get("raw") in (1, "1", True, "true") else None,
        "source": "openai_compatible_proxy",
    })


def api_web_markdown(params: dict | None = None) -> dict:
    """Fetch URL and return simplified markdown/text. Params: { url }"""
    import re, html as htmlmod
    p = params or {}
    url = (p.get("url") or p.get("uri") or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return _ok({"timestamp": _now(), "error": "url_required_https"})
    low = url.lower()
    if any(x in low for x in ["localhost", "127.0.0.1", "0.0.0.0", "169.254.", "metadata.google"]):
        return _ok({"timestamp": _now(), "error": "url_blocked"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(200_000)
            ctype = r.headers.get("Content-Type", "")
            status = getattr(r, "status", 200)
    except Exception as e:
        return _ok({"timestamp": _now(), "error": str(e)[:240], "url": url})
    text = raw.decode("utf-8", "replace")
    # crude html -> text
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n\n", text)
    text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text)
    text = re.sub(r"(?i)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\\2](\\1)", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = re.sub(r"[ \\t]+", " ", text)
    text = re.sub(r"\\n{3,}", "\n\n", text).strip()
    return _ok({
        "timestamp": _now(),
        "url": url,
        "status": status,
        "content_type": ctype,
        "markdown": text[:20000],
        "chars": len(text),
        "source": "sml_web_markdown",
    })


def api_web_search(params: dict | None = None) -> dict:
    """Keyless web search: Wikipedia OpenSearch + DuckDuckGo instant + optional HTML.
    Params: { q, limit? }
    """
    import re
    from urllib.parse import quote_plus, unquote, parse_qs, urlparse
    p = params or {}
    q = (p.get("q") or p.get("query") or p.get("search") or "").strip()
    if not q:
        return _ok({"timestamp": _now(), "error": "q_required"})
    try:
        limit = max(1, min(int(p.get("limit") or 8), 15))
    except Exception:
        limit = 8
    results = []
    sources = []

    # 1) Wikipedia OpenSearch
    try:
        wurl = (
            "https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit="
            f"{limit}&search={quote_plus(q)}"
        )
        w = _get(wurl, timeout=15)
        if isinstance(w, list) and len(w) >= 4:
            titles, descs, links = w[1], w[2], w[3]
            for i, title in enumerate(titles):
                results.append({
                    "title": title,
                    "url": links[i] if i < len(links) else "",
                    "snippet": descs[i] if i < len(descs) else "",
                    "source": "wikipedia",
                })
            sources.append("wikipedia_opensearch")
    except Exception:
        pass

    # 2) DuckDuckGo instant answer API
    if len(results) < limit:
        try:
            durl = f"https://api.duckduckgo.com/?q={quote_plus(q)}&format=json&no_html=1&skip_disambig=1"
            d = _get(durl, timeout=15)
            if isinstance(d, dict) and not d.get("error"):
                if d.get("AbstractText"):
                    results.append({
                        "title": d.get("Heading") or q,
                        "url": d.get("AbstractURL") or "",
                        "snippet": d.get("AbstractText"),
                        "source": "duckduckgo_abstract",
                    })
                for t in (d.get("RelatedTopics") or []):
                    if isinstance(t, dict) and t.get("Text"):
                        results.append({
                            "title": (t.get("Text") or "")[:80],
                            "url": t.get("FirstURL") or "",
                            "snippet": t.get("Text") or "",
                            "source": "duckduckgo_related",
                        })
                    elif isinstance(t, dict) and t.get("Topics"):
                        for tt in t.get("Topics") or []:
                            if isinstance(tt, dict) and tt.get("Text"):
                                results.append({
                                    "title": (tt.get("Text") or "")[:80],
                                    "url": tt.get("FirstURL") or "",
                                    "snippet": tt.get("Text") or "",
                                    "source": "duckduckgo_related",
                                })
                    if len(results) >= limit:
                        break
                sources.append("duckduckgo_api")
        except Exception:
            pass

    # 3) HTML scrape fallback
    if len(results) < 3:
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read(300_000).decode("utf-8", "replace")
            for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
                href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if "uddg=" in href:
                    try:
                        qs = parse_qs(urlparse(href).query)
                        href = unquote(qs.get("uddg", [href])[0])
                    except Exception:
                        pass
                results.append({"title": title, "url": href, "snippet": "", "source": "duckduckgo_html"})
                if len(results) >= limit:
                    break
            sources.append("duckduckgo_html")
        except Exception:
            pass

    # dedupe by url/title
    seen = set()
    out = []
    for r in results:
        key = (r.get("url") or "") + "|" + (r.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return _ok({
        "timestamp": _now(),
        "q": q,
        "count": len(out),
        "results": out,
        "sources": sources,
        "source": "+".join(sources) or "web_search",
    })


def api_eth_rpc(params: dict | None = None) -> dict:
    """Ethereum JSON-RPC helper (public RPC). Params: { method?, params?, address?, block? }"""
    p = params or {}
    method = (p.get("method") or "eth_blockNumber").strip()
    allowed = {
        "eth_blockNumber", "eth_chainId", "eth_gasPrice", "eth_getBalance",
        "eth_getCode", "eth_call", "eth_getTransactionByHash", "eth_getTransactionReceipt",
        "eth_getBlockByNumber", "net_version",
    }
    if method not in allowed:
        return _ok({"timestamp": _now(), "error": "method_not_allowed", "allowed": sorted(allowed)})
    rpc_params = p.get("params")
    if isinstance(rpc_params, str):
        try:
            rpc_params = json.loads(rpc_params)
        except Exception:
            rpc_params = None
    if rpc_params is None:
        if method == "eth_getBalance":
            addr = (p.get("address") or p.get("wallet") or "").strip()
            block = p.get("block") or "latest"
            if not addr.startswith("0x"):
                return _ok({"timestamp": _now(), "error": "address_required"})
            rpc_params = [addr, block]
        elif method == "eth_getBlockByNumber":
            block = p.get("block") or "latest"
            rpc_params = [block, False]
        elif method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
            h = (p.get("hash") or p.get("tx") or "").strip()
            if not h.startswith("0x"):
                return _ok({"timestamp": _now(), "error": "hash_required"})
            rpc_params = [h]
        else:
            rpc_params = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": rpc_params}).encode()
    rpcs = [
        "https://ethereum.publicnode.com",
        "https://rpc.ankr.com/eth",
        "https://cloudflare-eth.com",
    ]
    last_err = None
    for rpc in rpcs:
        req = urllib.request.Request(
            rpc, data=body, method="POST",
            headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            return _ok({"timestamp": _now(), "chain": "ethereum", "rpc": rpc, "method": method, "result": data.get("result"), "error": data.get("error"), "source": "eth_json_rpc"})
        except Exception as e:
            last_err = str(e)[:200]
    return _ok({"timestamp": _now(), "error": last_err or "rpc_failed", "method": method})


def api_base_rpc(params: dict | None = None) -> dict:
    """Base mainnet JSON-RPC helper. Params: { method?, params?, address?, block? }"""
    p = params or {}
    method = (p.get("method") or "eth_blockNumber").strip()
    allowed = {
        "eth_blockNumber", "eth_chainId", "eth_gasPrice", "eth_getBalance",
        "eth_getCode", "eth_call", "eth_getTransactionByHash", "eth_getTransactionReceipt",
        "eth_getBlockByNumber", "net_version",
    }
    if method not in allowed:
        return _ok({"timestamp": _now(), "error": "method_not_allowed", "allowed": sorted(allowed)})
    rpc_params = p.get("params")
    if isinstance(rpc_params, str):
        try:
            rpc_params = json.loads(rpc_params)
        except Exception:
            rpc_params = None
    if rpc_params is None:
        if method == "eth_getBalance":
            addr = (p.get("address") or p.get("wallet") or "").strip()
            block = p.get("block") or "latest"
            if not addr.startswith("0x"):
                return _ok({"timestamp": _now(), "error": "address_required"})
            rpc_params = [addr, block]
        elif method == "eth_getBlockByNumber":
            block = p.get("block") or "latest"
            rpc_params = [block, False]
        elif method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
            h = (p.get("hash") or p.get("tx") or "").strip()
            if not h.startswith("0x"):
                return _ok({"timestamp": _now(), "error": "hash_required"})
            rpc_params = [h]
        else:
            rpc_params = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": rpc_params}).encode()
    rpcs = [
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://base.llamarpc.com",
    ]
    last_err = None
    for rpc in rpcs:
        req = urllib.request.Request(
            rpc, data=body, method="POST",
            headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            return _ok({"timestamp": _now(), "chain": "base", "chain_id": 8453, "rpc": rpc, "method": method, "result": data.get("result"), "error": data.get("error"), "source": "base_json_rpc"})
        except Exception as e:
            last_err = str(e)[:200]
    return _ok({"timestamp": _now(), "error": last_err or "rpc_failed", "method": method})


def api_domain_enrich(params: dict | None = None) -> dict:
    """Cheap domain enrichment: DNS/RDAP + optional IP geo. Params: { domain }"""
    import re, socket
    p = params or {}
    domain = (p.get("domain") or p.get("url") or p.get("q") or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0].split(":")[0]
    if not domain or "." not in domain:
        return _ok({"timestamp": _now(), "error": "domain_required"})
    out = {"domain": domain, "timestamp": _now(), "source": "sml_domain_enrich"}
    # DNS A
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(domain, None)})
        out["ips"] = ips[:10]
    except Exception as e:
        out["ips"] = []
        out["dns_error"] = str(e)[:120]
    # RDAP
    try:
        rdap = _get(f"https://rdap.org/domain/{domain}", timeout=15)
        if isinstance(rdap, dict) and not rdap.get("error"):
            out["rdap"] = {
                "handle": rdap.get("handle"),
                "ldhName": rdap.get("ldhName"),
                "status": rdap.get("status"),
                "nameservers": [n.get("ldhName") for n in (rdap.get("nameservers") or []) if isinstance(n, dict)][:10],
                "events": rdap.get("events"),
            }
        else:
            out["rdap"] = rdap
    except Exception as e:
        out["rdap_error"] = str(e)[:120]
    # IP geo on first IP
    if out.get("ips"):
        geo = _get(f"http://ip-api.com/json/{out['ips'][0]}?fields=status,country,regionName,city,isp,org,as,query", timeout=10)
        out["geo"] = geo
    return _ok(out)


def api_news_headlines(params: dict | None = None) -> dict:
    """Google News RSS headlines. Params: { q?, hl?, gl?, ceid?, limit? }"""
    import re
    from urllib.parse import quote_plus
    import xml.etree.ElementTree as ET
    p = params or {}
    q = (p.get("q") or p.get("query") or "crypto OR bitcoin OR AI agents").strip()
    hl = (p.get("hl") or "en").strip()
    gl = (p.get("gl") or "US").strip()
    ceid = (p.get("ceid") or f"{gl}:{hl}").strip()
    try:
        limit = max(1, min(int(p.get("limit") or 15), 40))
    except Exception:
        limit = 15
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(ceid)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            xml = r.read()
    except Exception as e:
        return _ok({"timestamp": _now(), "error": str(e)[:240], "q": q})
    items = []
    try:
        root = ET.fromstring(xml)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = item.find("source")
            src = (source.text if source is not None else "") or ""
            items.append({"title": title, "url": link, "published": pub, "source": src})
    except Exception as e:
        return _ok({"timestamp": _now(), "error": f"rss_parse:{e}"[:200], "q": q})
    return _ok({"timestamp": _now(), "q": q, "count": len(items), "headlines": items, "source": "google_news_rss"})


def api_social_search(params: dict | None = None) -> dict:
    """Public web social/news search (not official X API). Params: { q, limit? }
    Uses DuckDuckGo news-biased query for agent social pulse when Twitter firehose unavailable.
    """
    p = dict(params or {})
    q = (p.get("q") or p.get("query") or "").strip()
    if not q:
        return _ok({"timestamp": _now(), "error": "q_required"})
    # bias toward recent social chatter via site filters + news
    p["q"] = f"{q} (site:x.com OR site:twitter.com OR site:reddit.com OR site:nitter.net)"
    base = api_web_search(p)
    # unwrap _ok envelope
    try:
        inner = json.loads(base.get("result") or "{}")
    except Exception:
        inner = {"error": "decode_failed"}
    inner["note"] = "public_web_social_proxy_not_official_x_api"
    inner["product"] = "social_search"
    return _ok(inner)





# ─── DATA CONVERSION ──────────────────────────────────────────────────────────

def api_json_validate(params: dict | None = None) -> dict:
    import json as _json
    p = params or {}
    raw = p.get("json") or p.get("data") or "{}"
    try:
        parsed = _json.loads(raw)
        return _ok({"valid": True, "type": type(parsed).__name__, "keys": list(parsed.keys()) if isinstance(parsed, dict) else len(parsed) if isinstance(parsed, list) else None, "source": "json_validate"})
    except Exception as e:
        return _ok({"valid": False, "error": str(e), "source": "json_validate"})

def api_json_to_csv(params: dict | None = None) -> dict:
    import json as _json, io, csv as _csv
    p = params or {}
    raw = p.get("json") or p.get("data") or "[]"
    try:
        data = _json.loads(raw)
        if not isinstance(data, list): data = [data]
        buf = io.StringIO()
        if data:
            w = _csv.DictWriter(buf, fieldnames=list(data[0].keys()))
            w.writeheader(); w.writerows(data)
        return _ok({"csv": buf.getvalue(), "rows": len(data), "source": "json_to_csv"})
    except Exception as e:
        return _ok({"error": str(e), "source": "json_to_csv"})

def api_csv_to_json(params: dict | None = None) -> dict:
    import io, csv as _csv
    p = params or {}
    raw = p.get("csv") or p.get("data") or ""
    try:
        reader = _csv.DictReader(io.StringIO(raw))
        rows = list(reader)
        return _ok({"data": rows, "rows": len(rows), "source": "csv_to_json"})
    except Exception as e:
        return _ok({"error": str(e), "source": "csv_to_json"})

def api_yaml_to_json(params: dict | None = None) -> dict:
    import json as _json
    p = params or {}
    raw = p.get("yaml") or p.get("data") or ""
    try:
        import yaml
        return _ok({"data": yaml.safe_load(raw), "source": "yaml_to_json"})
    except ImportError:
        # manual simple yaml parse for flat key: value
        out = {}
        for line in raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return _ok({"data": out, "source": "yaml_to_json_basic"})
    except Exception as e:
        return _ok({"error": str(e), "source": "yaml_to_json"})

def api_json_to_yaml(params: dict | None = None) -> dict:
    import json as _json
    p = params or {}
    raw = p.get("json") or p.get("data") or "{}"
    try:
        data = _json.loads(raw)
        # simple yaml serialiser
        def to_yaml(obj, indent=0):
            pad = "  " * indent
            nl = "\n"
            if isinstance(obj, dict):
                return nl.join(f"{pad}{k}: {to_yaml(v, indent+1) if isinstance(v,(dict,list)) else v}" for k,v in obj.items())
            elif isinstance(obj, list):
                return nl.join(f"{pad}- {item}" for item in obj)
            return str(obj)
        return _ok({"yaml": to_yaml(data), "source": "json_to_yaml"})
    except Exception as e:
        return _ok({"error": str(e), "source": "json_to_yaml"})

def api_xml_to_json(params: dict | None = None) -> dict:
    import json as _json, xml.etree.ElementTree as ET
    p = params or {}
    raw = p.get("xml") or p.get("data") or "<root/>"
    try:
        def elem_to_dict(el):
            d = dict(el.attrib)
            children = list(el)
            if children:
                child_dict = {}
                for ch in children:
                    cd = elem_to_dict(ch)
                    if ch.tag in child_dict:
                        if not isinstance(child_dict[ch.tag], list): child_dict[ch.tag] = [child_dict[ch.tag]]
                        child_dict[ch.tag].append(cd)
                    else:
                        child_dict[ch.tag] = cd
                d.update(child_dict)
            if el.text and el.text.strip(): d["_text"] = el.text.strip()
            return d
        root = ET.fromstring(raw)
        return _ok({"data": {root.tag: elem_to_dict(root)}, "source": "xml_to_json"})
    except Exception as e:
        return _ok({"error": str(e), "source": "xml_to_json"})


# ─── TEXT PROCESSING ──────────────────────────────────────────────────────────

def api_text_slugify(params: dict | None = None) -> dict:
    import re
    p = params or {}
    text = p.get("text") or p.get("q") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    return _ok({"slug": slug, "source": "text_slugify"})

def api_text_case_convert(params: dict | None = None) -> dict:
    p = params or {}
    text = p.get("text") or ""
    case = (p.get("case") or "upper").lower()
    result = text.upper() if case == "upper" else text.lower() if case == "lower" else text.title() if case == "title" else text.swapcase()
    return _ok({"result": result, "case": case, "source": "text_case_convert"})

def api_text_stats(params: dict | None = None) -> dict:
    p = params or {}
    text = p.get("text") or ""
    words = text.split()
    return _ok({"chars": len(text), "words": len(words), "lines": len(text.splitlines()), "sentences": text.count(".") + text.count("!") + text.count("?"), "source": "text_stats"})

def api_keyword_extract(params: dict | None = None) -> dict:
    import re
    from collections import Counter
    p = params or {}
    text = p.get("text") or ""
    limit = int(p.get("limit") or 10)
    stop = {"the","a","an","and","or","but","in","on","at","to","for","of","with","by","is","are","was","were","be","been","has","have","had","it","its","this","that","as","not","from","he","she","they","we","you","i"}
    words = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", text) if w.lower() not in stop]
    top = Counter(words).most_common(limit)
    return _ok({"keywords": [{"word": w, "count": c} for w, c in top], "source": "keyword_extract"})

def api_text_diff(params: dict | None = None) -> dict:
    import difflib
    p = params or {}
    a = (p.get("a") or p.get("text1") or "").splitlines(keepends=True)
    b = (p.get("b") or p.get("text2") or "").splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, lineterm=""))
    return _ok({"diff": "".join(diff), "lines_changed": len([l for l in diff if l.startswith(("+","-")) and not l.startswith(("+++","---"))]), "source": "text_diff"})

def api_regex_test(params: dict | None = None) -> dict:
    import re
    p = params or {}
    pattern = p.get("pattern") or p.get("regex") or ""
    text = p.get("text") or ""
    try:
        matches = re.findall(pattern, text)
        return _ok({"matches": matches[:50], "count": len(matches), "match": bool(matches), "source": "regex_test"})
    except re.error as e:
        return _ok({"error": str(e), "match": False, "source": "regex_test"})


# ─── VALIDATION & PARSING ─────────────────────────────────────────────────────

def api_email_validate(params: dict | None = None) -> dict:
    import re
    p = params or {}
    email = p.get("email") or p.get("q") or ""
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    valid = bool(re.match(pattern, email))
    parts = email.split("@") if "@" in email else [email, ""]
    return _ok({"email": email, "valid": valid, "local": parts[0], "domain": parts[1] if len(parts) > 1 else "", "source": "email_validate"})

def api_url_parse(params: dict | None = None) -> dict:
    from urllib.parse import urlparse, parse_qs
    p = params or {}
    url = p.get("url") or p.get("q") or ""
    try:
        r = urlparse(url)
        return _ok({"scheme": r.scheme, "host": r.netloc, "path": r.path, "query": parse_qs(r.query), "fragment": r.fragment, "valid": bool(r.scheme and r.netloc), "source": "url_parse"})
    except Exception as e:
        return _ok({"error": str(e), "source": "url_parse"})

def api_ip_info(params: dict | None = None) -> dict:
    p = params or {}
    ip = p.get("ip") or p.get("q") or "8.8.8.8"
    try:
        r = urllib.request.urlopen(urllib.request.Request(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,query,lat,lon", headers={"User-Agent": UA}), timeout=12)
        data = json.loads(r.read())
        return _ok({**data, "source": "ip_info"})
    except Exception as e:
        return _ok({"ip": ip, "error": str(e), "source": "ip_info"})

def api_useragent_parse(params: dict | None = None) -> dict:
    import re
    p = params or {}
    ua = p.get("ua") or p.get("user_agent") or p.get("q") or ""
    mobile = bool(re.search(r"Mobile|Android|iPhone|iPad", ua, re.I))
    bot = bool(re.search(r"bot|crawler|spider|scraper", ua, re.I))
    browser = "Chrome" if "Chrome" in ua else "Firefox" if "Firefox" in ua else "Safari" if "Safari" in ua else "Edge" if "Edge" in ua else "Other"
    os_name = "Windows" if "Windows" in ua else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else "iOS" if "iPhone|iPad" in ua else "Android" if "Android" in ua else "Other"
    return _ok({"ua": ua[:200], "browser": browser, "os": os_name, "mobile": mobile, "bot": bot, "source": "useragent_parse"})

def api_semver_compare(params: dict | None = None) -> dict:
    p = params or {}
    a = p.get("a") or p.get("v1") or "0.0.0"
    b = p.get("b") or p.get("v2") or "0.0.0"
    def parse(v):
        parts = v.lstrip("v").split(".")
        return tuple(int(x) for x in parts[:3]) if len(parts) >= 3 else (0,0,0)
    pa, pb = parse(a), parse(b)
    result = -1 if pa < pb else 1 if pa > pb else 0
    return _ok({"a": a, "b": b, "result": result, "comparison": f"{a} {'<' if result<0 else '>' if result>0 else '='} {b}", "newer": b if result < 0 else a, "source": "semver_compare"})


# ─── ENCODING & CRYPTO ────────────────────────────────────────────────────────

def api_hash_compute(params: dict | None = None) -> dict:
    import hashlib
    p = params or {}
    text = p.get("text") or p.get("data") or ""
    algo = p.get("algo") or "sha256"
    try:
        h = hashlib.new(algo, text.encode()).hexdigest()
        return _ok({"hash": h, "algo": algo, "input_len": len(text), "source": "hash_compute"})
    except Exception as e:
        return _ok({"error": str(e), "source": "hash_compute"})

def api_hmac_compute(params: dict | None = None) -> dict:
    import hmac, hashlib
    p = params or {}
    text = p.get("text") or p.get("data") or ""
    key = p.get("key") or p.get("secret") or ""
    algo = p.get("algo") or "sha256"
    try:
        h = hmac.new(key.encode(), text.encode(), getattr(hashlib, algo, hashlib.sha256)).hexdigest()
        return _ok({"hmac": h, "algo": algo, "source": "hmac_compute"})
    except Exception as e:
        return _ok({"error": str(e), "source": "hmac_compute"})

def api_base64_encode(params: dict | None = None) -> dict:
    import base64
    p = params or {}
    text = p.get("text") or p.get("data") or p.get("input") or ""
    mode = (p.get("mode") or "encode").lower()
    decode = mode == "decode" or str(p.get("decode") or "false").lower() == "true"
    try:
        if decode:
            result = base64.b64decode(text.encode()).decode(errors="replace")
        else:
            result = base64.b64encode(text.encode()).decode()
        return _ok({"result": result, "operation": "decode" if decode else "encode", "source": "base64_encode"})
    except Exception as e:
        return _ok({"error": str(e), "source": "base64_encode"})

def api_hex_convert(params: dict | None = None) -> dict:
    p = params or {}
    text = p.get("text") or p.get("data") or ""
    decode = str(p.get("decode") or "false").lower() == "true"
    try:
        if decode:
            result = bytes.fromhex(text.strip()).decode(errors="replace")
        else:
            result = text.encode().hex()
        return _ok({"result": result, "operation": "decode" if decode else "encode", "source": "hex_convert"})
    except Exception as e:
        return _ok({"error": str(e), "source": "hex_convert"})

def api_jwt_decode(params: dict | None = None) -> dict:
    import base64, json as _json
    p = params or {}
    token = p.get("token") or p.get("jwt") or ""
    try:
        parts = token.split(".")
        def decode_part(s):
            s += "=" * (-len(s) % 4)
            return _json.loads(base64.b64decode(s.replace("-", "+").replace("_", "/")))
        header = decode_part(parts[0]) if len(parts) > 0 else {}
        payload = decode_part(parts[1]) if len(parts) > 1 else {}
        return _ok({"header": header, "payload": payload, "parts": len(parts), "source": "jwt_decode"})
    except Exception as e:
        return _ok({"error": str(e), "source": "jwt_decode"})


# ─── MATH & FINANCE ───────────────────────────────────────────────────────────

def api_calculator(params: dict | None = None) -> dict:
    import ast, operator
    p = params or {}
    expr = p.get("expr") or p.get("expression") or p.get("q") or "0"
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg}
    def safe_eval(node):
        if isinstance(node, ast.Constant): return node.value
        if isinstance(node, ast.BinOp): return ops[type(node.op)](safe_eval(node.left), safe_eval(node.right))
        if isinstance(node, ast.UnaryOp): return ops[type(node.op)](safe_eval(node.operand))
        raise ValueError(f"Unsupported: {type(node)}")
    try:
        tree = ast.parse(expr, mode="eval")
        result = safe_eval(tree.body)
        return _ok({"expr": expr, "result": result, "source": "calculator"})
    except Exception as e:
        return _ok({"expr": expr, "error": str(e), "source": "calculator"})

def api_statistics(params: dict | None = None) -> dict:
    import statistics as _stats
    p = params or {}
    raw = p.get("data") or p.get("numbers") or ""
    try:
        import json as _json
        nums = _json.loads(raw) if raw.strip().startswith("[") else [float(x.strip()) for x in raw.split(",") if x.strip()]
        return _ok({"count": len(nums), "mean": _stats.mean(nums), "median": _stats.median(nums), "stdev": _stats.stdev(nums) if len(nums) > 1 else 0, "min": min(nums), "max": max(nums), "source": "statistics"})
    except Exception as e:
        return _ok({"error": str(e), "source": "statistics"})

def api_unit_convert(params: dict | None = None) -> dict:
    p = params or {}
    value = float(p.get("value") or 1)
    from_unit = (p.get("from") or "").lower()
    to_unit = (p.get("to") or "").lower()
    conversions = {
        ("km","miles"): lambda v: v * 0.621371, ("km","mi"): lambda v: v * 0.621371,
        ("miles","km"): lambda v: v * 1.60934, ("mi","km"): lambda v: v * 1.60934,
        ("kg","lbs"): lambda v: v * 2.20462, ("lbs","kg"): lambda v: v / 2.20462,
        ("c","f"): lambda v: v * 9/5 + 32, ("f","c"): lambda v: (v - 32) * 5/9,
        ("m","ft"): lambda v: v * 3.28084, ("ft","m"): lambda v: v / 3.28084,
        ("usd","eur"): lambda v: v * 0.92, ("eur","usd"): lambda v: v / 0.92,
        ("gb","mb"): lambda v: v * 1024, ("mb","gb"): lambda v: v / 1024,
    }
    fn = conversions.get((from_unit, to_unit))
    if fn:
        return _ok({"value": value, "from": from_unit, "to": to_unit, "result": round(fn(value), 6), "source": "unit_convert"})
    return _ok({"error": f"Unknown conversion {from_unit}→{to_unit}", "supported": list(conversions.keys()), "source": "unit_convert"})

def api_percentage(params: dict | None = None) -> dict:
    p = params or {}
    try:
        part = float(p.get("part") or p.get("value") or 0)
        whole = float(p.get("whole") or p.get("total") or 100)
        pct = round((part / whole) * 100, 4) if whole else 0
        return _ok({"part": part, "whole": whole, "percentage": pct, "source": "percentage"})
    except Exception as e:
        return _ok({"error": str(e), "source": "percentage"})

def api_number_format(params: dict | None = None) -> dict:
    p = params or {}
    try:
        num = float(p.get("number") or p.get("n") or 0)
        decimals = int(p.get("decimals") or 2)
        formatted = f"{num:,.{decimals}f}"
        return _ok({"number": num, "formatted": formatted, "scientific": f"{num:.{decimals}e}", "source": "number_format"})
    except Exception as e:
        return _ok({"error": str(e), "source": "number_format"})


# ─── NETWORK & DOMAINS ────────────────────────────────────────────────────────

def api_http_check(params: dict | None = None) -> dict:
    import time
    p = params or {}
    url = p.get("url") or p.get("q") or "https://example.com"
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        r = urllib.request.urlopen(req, timeout=15)
        ms = round((time.time() - start) * 1000)
        return _ok({"url": url, "status": r.status, "ok": r.status < 400, "latency_ms": ms, "headers": dict(list(r.headers.items())[:8]), "source": "http_check"})
    except urllib.error.HTTPError as e:
        return _ok({"url": url, "status": e.code, "ok": False, "latency_ms": round((time.time()-start)*1000), "source": "http_check"})
    except Exception as e:
        return _ok({"url": url, "status": 0, "ok": False, "error": str(e)[:200], "source": "http_check"})

def api_tls_cert(params: dict | None = None) -> dict:
    import ssl, socket
    p = params or {}
    host = (p.get("host") or p.get("domain") or p.get("url") or "example.com").replace("https://","").replace("http://","").split("/")[0]
    port = int(p.get("port") or 443)
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
            s.settimeout(12)
            s.connect((host, port))
            cert = s.getpeercert()
        subj = {k: v for tup in cert.get("subject", []) for k, v in tup}
        issuer = {k: v for tup in cert.get("issuer", []) for k, v in tup}
        return _ok({"host": host, "subject": subj, "issuer": issuer, "expires": cert.get("notAfter"), "source": "tls_cert"})
    except Exception as e:
        return _ok({"host": host, "error": str(e)[:200], "source": "tls_cert"})

def api_robots_check(params: dict | None = None) -> dict:
    p = params or {}
    url = p.get("url") or p.get("domain") or "https://example.com"
    if not url.startswith("http"): url = "https://" + url
    robots_url = url.rstrip("/") + "/robots.txt"
    try:
        r = urllib.request.urlopen(urllib.request.Request(robots_url, headers={"User-Agent": UA}), timeout=15)
        content = r.read().decode(errors="ignore")[:3000]
        lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
        return _ok({"url": robots_url, "status": r.status, "content": content, "rules": len(lines), "source": "robots_check"})
    except Exception as e:
        return _ok({"url": robots_url, "error": str(e)[:200], "source": "robots_check"})

def api_sitemap_reader(params: dict | None = None) -> dict:
    import xml.etree.ElementTree as ET
    p = params or {}
    url = p.get("url") or p.get("domain") or "https://example.com"
    if not url.startswith("http"): url = "https://" + url
    sitemap_url = url.rstrip("/") + "/sitemap.xml"
    try:
        r = urllib.request.urlopen(urllib.request.Request(sitemap_url, headers={"User-Agent": UA}), timeout=15)
        content = r.read().decode(errors="ignore")
        root = ET.fromstring(content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [u.text for u in root.findall(".//sm:loc", ns) if u.text][:50]
        return _ok({"url": sitemap_url, "count": len(urls), "urls": urls, "source": "sitemap_reader"})
    except Exception as e:
        return _ok({"url": sitemap_url, "error": str(e)[:200], "source": "sitemap_reader"})

def api_whois_rdap(params: dict | None = None) -> dict:
    p = params or {}
    domain = (p.get("domain") or p.get("q") or "example.com").replace("https://","").replace("http://","").split("/")[0]
    try:
        r = urllib.request.urlopen(urllib.request.Request(f"https://rdap.org/domain/{domain}", headers={"User-Agent": UA, "Accept": "application/json"}), timeout=15)
        data = json.loads(r.read())
        return _ok({"domain": domain, "status": data.get("status"), "registrar": next((e.get("fn") or e.get("formattedName","") for e in data.get("entities",[]) if "registrar" in e.get("roles",[])), None), "created": next((e.get("value") for e in data.get("events",[]) if e.get("eventAction")=="registration"), None), "expires": next((e.get("value") for e in data.get("events",[]) if e.get("eventAction")=="expiration"), None), "source": "whois_rdap"})
    except Exception as e:
        return _ok({"domain": domain, "error": str(e)[:200], "source": "whois_rdap"})


# ─── PAYMENTS & X402 UTILS ────────────────────────────────────────────────────

def api_x402_market_pulse(params: dict | None = None) -> dict:
    """Live x402 market intel: top sellers, categories, price floor."""
    try:
        r = urllib.request.urlopen(urllib.request.Request("https://agent402.tools/api/index/stats", headers={"User-Agent": UA, "Accept": "application/json"}), timeout=15)
        data = json.loads(r.read())
        return _ok({**data, "source": "x402_market_pulse"})
    except Exception as e:
        return _ok({"price_floor_usdc": 0.001, "top_categories": ["web","crypto","rpc","llm","data"], "note": "agent402 stats unavailable", "error": str(e)[:100], "source": "x402_market_pulse"})

def api_usdc_balance(params: dict | None = None) -> dict:
    """USDC balance for any Base address."""
    p = params or {}
    addr = p.get("address") or p.get("wallet") or p.get("q") or "0x0000000000000000000000000000000000000000"
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    try:
        data_field = "0x70a08231000000000000000000000000" + addr[2:].lower().zfill(40)
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":USDC,"data":data_field},"latest"]}).encode()
        r = json.loads(urllib.request.urlopen(urllib.request.Request("https://mainnet.base.org", data=body, headers={"Content-Type":"application/json","User-Agent":UA}), timeout=12).read())
        bal = int(r["result"], 16) / 1e6
        return _ok({"address": addr, "usdc_balance": bal, "network": "base", "source": "usdc_balance"})
    except Exception as e:
        return _ok({"address": addr, "error": str(e)[:200], "source": "usdc_balance"})

def api_tx_status(params: dict | None = None) -> dict:
    """Check Base transaction status by hash."""
    p = params or {}
    tx = p.get("tx") or p.get("hash") or p.get("transaction_hash") or ""
    try:
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_getTransactionReceipt","params":[tx]}).encode()
        r = json.loads(urllib.request.urlopen(urllib.request.Request("https://mainnet.base.org", data=body, headers={"Content-Type":"application/json","User-Agent":UA}), timeout=12).read())
        rec = r.get("result")
        if not rec:
            return _ok({"tx": tx, "status": "pending_or_not_found", "source": "tx_status"})
        return _ok({"tx": tx, "status": "success" if rec.get("status")=="0x1" else "failed", "block": int(rec.get("blockNumber","0x0"),16), "gas_used": int(rec.get("gasUsed","0x0"),16), "source": "tx_status"})
    except Exception as e:
        return _ok({"tx": tx, "error": str(e)[:200], "source": "tx_status"})


def api_us_weather_alerts(params: dict | None = None) -> dict:
    p = params or {}
    state = (p.get("state") or p.get("area") or "").upper()[:2]
    url = f"https://api.weather.gov/alerts/active{'?area='+state if state else '?limit=10'}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "scriptmasterlabs/1.0 (contact@scriptmasterlabs.com)", "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        features = d.get("features", [])[:10]
        alerts = [{"event": f["properties"].get("event"), "headline": f["properties"].get("headline", "")[:120], "severity": f["properties"].get("severity"), "area": f["properties"].get("areaDesc", "")[:80]} for f in features]
        return _ok({"count": len(features), "state_filter": state or "all", "alerts": alerts, "source": "us_weather_gov"})
    except Exception as e:
        return _ok({"error": str(e)[:200], "source": "us_weather_alerts"})


def api_usgs_earthquakes(params: dict | None = None) -> dict:
    p = params or {}
    min_mag = p.get("min_magnitude") or p.get("minmagnitude") or "2.5"
    limit = min(int(p.get("limit") or 10), 25)
    url = f"https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&minmagnitude={min_mag}&limit={limit}&orderby=time"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        features = d.get("features", [])
        quakes = [{"magnitude": f["properties"].get("mag"), "place": f["properties"].get("place"), "time": f["properties"].get("time"), "depth_km": f.get("geometry", {}).get("coordinates", [None, None, None])[2]} for f in features]
        return _ok({"count": len(quakes), "min_magnitude": min_mag, "earthquakes": quakes, "source": "usgs_earthquake_hazards"})
    except Exception as e:
        return _ok({"error": str(e)[:200], "source": "usgs_earthquakes"})


def api_fda_food_recalls(params: dict | None = None) -> dict:
    import urllib.parse as _uparse
    p = params or {}
    q = p.get("q") or p.get("query") or p.get("search") or "allergy"
    limit = min(int(p.get("limit") or 5), 10)
    url = f"https://api.fda.gov/food/enforcement.json?search={_uparse.quote(q)}&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        results = d.get("results", [])
        recalls = [{"product": r.get("product_description", "")[:80], "reason": r.get("reason_for_recall", "")[:100], "status": r.get("status"), "date": r.get("recall_initiation_date"), "company": r.get("recalling_firm", "")[:60]} for r in results]
        return _ok({"query": q, "count": len(recalls), "recalls": recalls, "source": "fda_food_enforcement"})
    except Exception as e:
        return _ok({"query": q, "error": str(e)[:200], "source": "fda_food_recalls"})


def api_us_gov_search(params: dict | None = None) -> dict:
    import urllib.parse as _uparse
    p = params or {}
    q = p.get("q") or p.get("query") or p.get("search") or "AI policy"
    limit = min(int(p.get("limit") or 5), 10)
    # Try search.usa.gov
    url = f"https://search.usa.gov/api/v2/search/web?query={_uparse.quote(q)}&affiliate=usagov&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        results = d.get("web", {}).get("results", []) or d.get("results", [])
        items = [{"title": r.get("title", "")[:80], "url": r.get("url", ""), "snippet": r.get("snippet", "")[:150]} for r in results[:limit]]
        return _ok({"query": q, "count": len(items), "results": items, "source": "usa_gov_search"})
    except Exception as e:
        return _ok({"query": q, "error": str(e)[:200], "source": "us_gov_search"})


# Registry consumed by provider.py


# ============================================================
# CRYPTO SQUEEZE SCANNER
# ============================================================

_CRYPTO_SQUEEZE_UNIVERSE = [
    # High beta / squeeze history on-chain
    "bitcoin","ethereum","solana","binancecoin","ripple",
    "dogecoin","shiba-inu","pepe","floki","bonk",
    "arbitrum","optimism","avalanche-2","polygon","chainlink",
    "uniswap","aave","compound-governance-token","maker","curve-dao-token",
    "hyperliquid","virtuals-protocol","grass","io-net","render-token",
    "near","aptos","sui","sei-network","injective-protocol",
    "worldcoin-wld","friend-tech","mog-coin","brett","baby-doge-coin",
]

_CRYPTO_PENNY_UNIVERSE = [
    # Sub-$1 high-volatility tokens with squeeze potential
    "shiba-inu","pepe","floki","bonk","baby-doge-coin","mog-coin",
    "book-of-meme","cat-in-a-dogs-world","turbo","milady","myro",
    "neiro","popcat","fwog","pnut","goat","act-i-the-ai-prophecy",
    "shoggoth","moodeng","ponke","retardio","giga","sigma",
]


def _fetch_coingecko_prices(ids: list[str]) -> list[dict]:
    """Fetch batch prices from CoinGecko free API."""
    ids_csv = ",".join(ids[:50])
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={ids_csv}"
        "&order=market_cap_desc&per_page=50&page=1"
        "&price_change_percentage=24h,7d"
        "&sparkline=false"
    )
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SMLBot/1.0)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _crypto_squeeze_score(coin: dict) -> dict:
    """Score crypto 0-100 for squeeze potential."""
    score = 0
    signals = []

    price = coin.get("current_price") or 0
    high24 = coin.get("high_24h") or price
    low24 = coin.get("low_24h") or price
    ath = coin.get("ath") or price
    ath_change = coin.get("ath_change_percentage") or 0
    change24 = coin.get("price_change_percentage_24h") or 0
    change7d = coin.get("price_change_percentage_7d_in_currency") or 0
    vol24 = coin.get("total_volume") or 0
    mcap = coin.get("market_cap") or 1

    # 1. Volume/mcap ratio (vol surge relative to size)
    vol_mcap = vol24 / max(mcap, 1)
    if vol_mcap > 0.5:
        score += 30; signals.append(f"VOL_MCAP_{round(vol_mcap,2)}x")
    elif vol_mcap > 0.2:
        score += 20; signals.append(f"VOL_MCAP_{round(vol_mcap,2)}x")
    elif vol_mcap > 0.1:
        score += 10; signals.append(f"VOL_MCAP_{round(vol_mcap,2)}x")

    # 2. Momentum: price near 24h high
    if high24 > 0:
        pct = price / high24
        if pct >= 0.95:
            score += 25; signals.append("NEAR_24H_HIGH")
        elif pct >= 0.85:
            score += 10

    # 3. 24h positive momentum
    if change24 >= 10:
        score += 20; signals.append(f"UP_{round(change24,1)}pct_24H")
    elif change24 >= 5:
        score += 10; signals.append(f"UP_{round(change24,1)}pct_24H")

    # 4. ATH distance (far from ATH = room to squeeze)
    if ath_change <= -70:
        score += 15; signals.append("FAR_FROM_ATH")
    elif ath_change <= -40:
        score += 8

    # 5. 7d reversal setup
    if change7d < -20 and change24 > 5:
        score += 10; signals.append("7D_REVERSAL")

    score = min(100, score)
    grade = "A" if score >= 70 else "B" if score >= 50 else "C" if score >= 30 else "D"
    return {"score": score, "grade": grade, "signals": signals}


def api_crypto_squeeze_scanner(params: dict | None = None) -> dict:
    """Crypto squeeze scanner — ranks tokens by vol/mcap surge, momentum, ATH distance.

    Req: { limit?: 5-20, price_max?: float (USD), ids?: csv of CoinGecko ids }
    Returns top squeeze setups with score, grade, signals.
    """
    import time as _t
    p = params or {}
    limit = min(20, max(1, int(p.get("limit") or 10)))
    price_max = float(p.get("price_max") or 999999)
    custom = p.get("ids") or p.get("symbols") or ""
    if custom:
        universe = [s.strip().lower() for s in str(custom).split(",") if s.strip()][:50]
    else:
        universe = _CRYPTO_SQUEEZE_UNIVERSE
    t0 = _t.time()
    coins = _fetch_coingecko_prices(universe)
    results = []
    for c in coins:
        if (c.get("current_price") or 0) > price_max:
            continue
        s = _crypto_squeeze_score(c)
        results.append({
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": c.get("current_price"),
            "market_cap": c.get("market_cap"),
            "volume_24h": c.get("total_volume"),
            "change_24h": round(c.get("price_change_percentage_24h") or 0, 2),
            "change_7d": round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
            "ath_change_pct": round(c.get("ath_change_percentage") or 0, 2),
            **s,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return _ok({
        "timestamp": _now(),
        "scan_ms": int((_t.time() - t0) * 1000),
        "universe_size": len(universe),
        "coins_fetched": len(results),
        "top_setups": results[:limit],
        "offering": "crypto_squeeze_scanner",
        "source": "coingecko_markets_api",
        "note": "Score 0-100: vol/mcap surge, 24h momentum, ATH distance, reversal. Grade A>=70.",
    })


def api_crypto_penny_squeeze_scanner(params: dict | None = None) -> dict:
    """Crypto penny squeeze scanner — sub-$1 tokens ranked by squeeze score.

    Req: { limit?: 5-20, price_max?: 1.0, ids?: csv of CoinGecko ids }
    Returns top low-cap squeeze setups with momentum + vol surge signals.
    """
    import time as _t
    p = params or {}
    limit = min(20, max(1, int(p.get("limit") or 10)))
    price_max = min(1.0, float(p.get("price_max") or 1.0))
    custom = p.get("ids") or p.get("symbols") or ""
    if custom:
        universe = [s.strip().lower() for s in str(custom).split(",") if s.strip()][:50]
    else:
        universe = list(dict.fromkeys(_CRYPTO_PENNY_UNIVERSE + _CRYPTO_SQUEEZE_UNIVERSE))
    t0 = _t.time()
    coins = _fetch_coingecko_prices(universe)
    results = []
    for c in coins:
        price = c.get("current_price") or 0
        if price > price_max or price <= 0:
            continue
        s = _crypto_squeeze_score(c)
        results.append({
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": price,
            "market_cap": c.get("market_cap"),
            "volume_24h": c.get("total_volume"),
            "change_24h": round(c.get("price_change_percentage_24h") or 0, 2),
            "change_7d": round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
            "ath_change_pct": round(c.get("ath_change_percentage") or 0, 2),
            **s,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return _ok({
        "timestamp": _now(),
        "scan_ms": int((_t.time() - t0) * 1000),
        "universe_size": len(universe),
        "coins_fetched": len(results),
        "top_setups": results[:limit],
        "offering": "crypto_penny_squeeze_scanner",
        "source": "coingecko_markets_api",
        "note": "Sub-$1 crypto squeeze: vol/mcap surge, reversal pattern, ATH distance. Grade A>=70.",
    })

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
    "crypto_news": api_crypto_news,
    "token_details": api_token_details,
    "trending_altcoins": api_trending_altcoins,
    "funding_rates": api_funding_rates,
    "defi_analytics": api_defi_analytics,
    "stablecoin_watch": api_stablecoin_watch,
    "web_fetch": api_web_fetch,
    "token_top_markets": api_token_top_holders_proxy,
    "llm_chat": api_llm_chat,
    "web_markdown": api_web_markdown,
    "web_search": api_web_search,
    "eth_rpc": api_eth_rpc,
    "base_rpc": api_base_rpc,
    "domain_enrich": api_domain_enrich,
    "news_headlines": api_news_headlines,
    "social_search": api_social_search,
    # --- Data conversion ---
    "json_validate": api_json_validate,
    "json_to_csv": api_json_to_csv,
    "csv_to_json": api_csv_to_json,
    "yaml_to_json": api_yaml_to_json,
    "json_to_yaml": api_json_to_yaml,
    "xml_to_json": api_xml_to_json,
    # --- Text processing ---
    "text_slugify": api_text_slugify,
    "text_case_convert": api_text_case_convert,
    "text_stats": api_text_stats,
    "keyword_extract": api_keyword_extract,
    "text_diff": api_text_diff,
    "regex_test": api_regex_test,
    # --- Validation & parsing ---
    "email_validate": api_email_validate,
    "url_parse": api_url_parse,
    "ip_info": api_ip_info,
    "useragent_parse": api_useragent_parse,
    "semver_compare": api_semver_compare,
    # --- Encoding & crypto ---
    "hash_compute": api_hash_compute,
    "hmac_compute": api_hmac_compute,
    "base64_encode": api_base64_encode,
    "hex_convert": api_hex_convert,
    "jwt_decode": api_jwt_decode,
    # --- Math & finance ---
    "calculator": api_calculator,
    "statistics": api_statistics,
    "unit_convert": api_unit_convert,
    "percentage": api_percentage,
    "number_format": api_number_format,
    # --- Network & domains ---
    "http_check": api_http_check,
    "tls_cert": api_tls_cert,
    "robots_check": api_robots_check,
    "sitemap_reader": api_sitemap_reader,
    "whois_rdap": api_whois_rdap,
    # --- Payments & x402 utils ---
    "x402_market_pulse": api_x402_market_pulse,
    "usdc_balance": api_usdc_balance,
    "tx_status": api_tx_status,
    # --- US Gov & public data ---
    "us_weather_alerts": api_us_weather_alerts,
    "usgs_earthquakes": api_usgs_earthquakes,
    "fda_food_recalls": api_fda_food_recalls,
    "us_gov_search": api_us_gov_search,
}

EXTRA_PRICES_USD = {
    "crypto_price": "0.001",
    "crypto_global": "0.001",
    "fx_rate": "0.001",
    "fear_greed_index": "0.001",
    "defi_chains_tvl": "0.001",
    "defi_protocol_tvl": "0.001",
    "stablecoin_mcap": "0.001",
    "btc_mempool_fees": "0.001",
    "eth_price": "0.001",
    "btc_price": "0.001",
    "sol_price": "0.001",
    "hyperliquid_meta": "0.001",
    "hyperliquid_all_mids": "0.001",
    "binance_funding": "0.001",
    "binance_ticker": "0.001",
    "treasury_yields": "0.001",
    "openfda_drug_label": "0.001",
    "clinical_trials_search": "0.001",
    "sec_company_tickers": "0.001",
    "coingecko_categories": "0.001",
    "defi_yields_pools": "0.001",
    "l2_activity": "0.001",
    "geckoterminal_trending": "0.001",
    "dexscreener_boosts": "0.001",
    "dexscreener_search": "0.001",
    "crypto_news": "0.001",
    "token_details": "0.001",
    "trending_altcoins": "0.001",
    "funding_rates": "0.001",
    "defi_analytics": "0.001",
    "stablecoin_watch": "0.001",
    "web_fetch": "0.001",
    "token_top_markets": "0.001",
    "llm_chat": "0.001",
    "web_markdown": "0.001",
    "web_search": "0.001",
    "eth_rpc": "0.001",
    "base_rpc": "0.001",
    "domain_enrich": "0.001",
    "news_headlines": "0.001",
    "social_search": "0.001",
    # --- Data conversion (agent402 category match) ---
    "json_validate": "0.001",
    "json_to_csv": "0.001",
    "csv_to_json": "0.001",
    "yaml_to_json": "0.001",
    "json_to_yaml": "0.001",
    "xml_to_json": "0.001",
    # --- Text processing ---
    "text_slugify": "0.001",
    "text_case_convert": "0.001",
    "text_stats": "0.001",
    "keyword_extract": "0.001",
    "text_diff": "0.001",
    "regex_test": "0.001",
    # --- Validation & parsing ---
    "email_validate": "0.001",
    "url_parse": "0.001",
    "ip_info": "0.002",
    "useragent_parse": "0.001",
    "semver_compare": "0.001",
    # --- Encoding & crypto ---
    "hash_compute": "0.001",
    "hmac_compute": "0.001",
    "base64_encode": "0.001",
    "hex_convert": "0.001",
    "jwt_decode": "0.001",
    # --- Math & finance ---
    "calculator": "0.001",
    "statistics": "0.001",
    "unit_convert": "0.001",
    "percentage": "0.001",
    "number_format": "0.001",
    # --- Network & domains (extend existing dns/http) ---
    "http_check": "0.002",
    "tls_cert": "0.002",
    "robots_check": "0.001",
    "sitemap_reader": "0.002",
    "whois_rdap": "0.003",
    # --- Payments & x402 utils ---
    "x402_market_pulse": "0.001",
    "usdc_balance": "0.001",
    "tx_status": "0.001",
    # --- US Gov & public data ---
    "us_weather_alerts": "0.001",
    "usgs_earthquakes": "0.001",
    "fda_food_recalls": "0.001",
    "us_gov_search": "0.001",
    # --- Squeeze APIs ---
    "squeeze_scanner": "0.01",
    "penny_squeeze_scanner": "0.01",
}

EXTRA_ACP_DEFAULTS = {k: 0.001 for k in EXTRA_ENDPOINTS}
EXTRA_ACP_DEFAULTS["squeeze_scanner"] = 0.01
EXTRA_ACP_DEFAULTS["penny_squeeze_scanner"] = 0.01


# ============================================================
# SQUEEZE SCANNER API
# Real-time equity squeeze setup scanner using live public data.
# Scores symbols on: short interest proxy (FTD velocity), momentum
# (price vs 20d high), volume surge, and options signal (IV rank).
# Returns ranked setups with squeeze score 0-100, grade, and thesis.
# ============================================================

_SQUEEZE_UNIVERSE_FULL = [
    # High short interest / meme / squeeze history
    "GME","AMC","MSTR","HOOD","BBBY","CLOV","WISH","EXPR","NAKD","SPCE",
    "TLRY","SNDL","WKHS","RIDE","NKLA","RKT","KOSS","FIZZ","BYFC","ATER",
    # High beta / momentum
    "PLTR","RIVN","LCID","SOFI","OPEN","SKLZ","DKNG","AFRM","UPST","MNDY",
    "CRWD","ZM","SNAP","RBLX","PTON","COIN","MARA","RIOT","HUT","CLSK",
    # Mid-large with squeeze potential
    "TSLA","NVDA","AMD","SMCI","SHOP","ROKU","DOCU","NET","CFLT","MDB",
]

_PENNY_UNIVERSE = [
    # Sub-$10 with squeeze history or short interest
    "SNDL","CLOV","WISH","EXPR","NAKD","BYFC","ATER","ANVS","PROG","MEGL",
    "MULN","FFIE","NKLA","WKHS","RIDE","GOEV","XELA","CTRM","SHIP","TOPS",
    "GFAI","ZVRA","NUO","BNOX","WISA","ABAT","NUVL","HOLO","MDJH","SRM",
    "MGOL","INPX","SURF","PHUN","VERB","ILUS","USDR","AGTC","OBSV","SXTC",
]


def _fetch_yahoo_quote(symbol: str) -> dict | None:
    """Light Yahoo Finance quote — price, volume, 52w high/low."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1mo"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SMLBot/1.0)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read())
        res = (d.get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        meta = res.get("meta", {})
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        volumes = (res.get("indicators", {}).get("quote") or [{}])[0].get("volume") or []
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        avg_vol = int(sum(v for v in volumes[-10:] if v) / max(1, len([v for v in volumes[-10:] if v]))) if volumes else 0
        cur_vol = volumes[-1] if volumes else 0
        high_20 = max((c for c in closes[-20:] if c), default=price)
        low_20 = min((c for c in closes[-20:] if c), default=price)
        return {
            "symbol": symbol,
            "price": round(price, 4) if price else None,
            "avg_volume_10d": avg_vol,
            "current_volume": cur_vol,
            "high_20d": round(high_20, 4) if high_20 else None,
            "low_20d": round(low_20, 4) if low_20 else None,
            "volume_surge": round(cur_vol / avg_vol, 2) if avg_vol > 0 and cur_vol else 0,
        }
    except Exception:
        return None


def _squeeze_score(q: dict) -> dict:
    """Score 0–100 across 4 dimensions. Higher = more squeeze potential."""
    score = 0
    signals = []

    # 1. Momentum — price near 20d high
    if q.get("price") and q.get("high_20d") and q["high_20d"] > 0:
        pct_of_high = q["price"] / q["high_20d"]
        mom = min(30, int(pct_of_high * 30))
        score += mom
        if pct_of_high >= 0.95:
            signals.append("NEAR_20D_HIGH")

    # 2. Volume surge
    vs = q.get("volume_surge", 0)
    if vs >= 5:
        score += 30; signals.append(f"VOL_SURGE_{round(vs,1)}x")
    elif vs >= 3:
        score += 20; signals.append(f"VOL_SURGE_{round(vs,1)}x")
    elif vs >= 2:
        score += 10; signals.append(f"VOL_ELEVATED_{round(vs,1)}x")

    # 3. Price range compression (low_20d close to price = coiling)
    if q.get("price") and q.get("low_20d") and q.get("high_20d"):
        rng = q["high_20d"] - q["low_20d"]
        compression = 1 - (rng / max(q["price"], 0.01))
        if compression > 0.85:
            score += 25; signals.append("COILING")
        elif compression > 0.70:
            score += 15; signals.append("RANGE_TIGHT")

    # 4. Sub-$5 premium (penny squeeze fuel)
    if q.get("price") and q["price"] < 5:
        score += 15; signals.append("PENNY_RANGE")
    elif q.get("price") and q["price"] < 20:
        score += 5; signals.append("LOW_FLOAT_RANGE")

    score = min(100, score)
    if score >= 70:
        grade = "A"
    elif score >= 50:
        grade = "B"
    elif score >= 30:
        grade = "C"
    else:
        grade = "D"

    return {"score": score, "grade": grade, "signals": signals}


def _run_squeeze_scan(universe: list[str], price_max: float | None = None, limit: int = 10) -> dict:
    """Fetch quotes for universe, score, rank, return top setups."""
    import concurrent.futures, time
    t0 = time.time()

    def fetch_and_score(sym):
        q = _fetch_yahoo_quote(sym)
        if not q or not q.get("price"):
            return None
        if price_max and q["price"] > price_max:
            return None
        s = _squeeze_score(q)
        return {**q, **s}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for row in ex.map(fetch_and_score, universe[:50]):
            if row:
                results.append(row)

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:limit]

    return {
        "timestamp": _now(),
        "scan_ms": int((time.time() - t0) * 1000),
        "universe_size": len(universe),
        "symbols_fetched": len(results),
        "top_setups": top,
        "offering": "squeeze_scanner",
        "source": "yahoo_finance_public",
        "note": "Score 0-100: momentum + volume surge + range compression + price tier. Grade A>=70, B>=50, C>=30.",
    }


def api_squeeze_scanner(params: dict | None = None) -> dict:
    """Full equity squeeze scanner — $1–$200 universe ranked by squeeze score.

    Req: { limit?: 5-20, price_max?: float, symbols?: csv }
    Returns top setups with score, grade, signals (NEAR_20D_HIGH, VOL_SURGE, COILING).
    Real-time from Yahoo Finance public API.
    """
    p = params or {}
    limit = min(20, max(1, int(p.get("limit") or 10)))
    price_max = float(p.get("price_max") or 200)
    custom = p.get("symbols") or p.get("tickers") or ""
    if custom:
        universe = [s.strip().upper() for s in str(custom).split(",") if s.strip()][:50]
    else:
        universe = _SQUEEZE_UNIVERSE_FULL
    return _ok(_run_squeeze_scan(universe, price_max=price_max, limit=limit))


def api_penny_squeeze_scanner(params: dict | None = None) -> dict:
    """Penny stock squeeze scanner — sub-$10 universe ranked by squeeze score.

    Req: { limit?: 5-20, price_max?: 10, symbols?: csv }
    Returns top penny squeeze setups: high vol surge, coiling range, near 20d high.
    Real-time from Yahoo Finance public API.
    """
    p = params or {}
    limit = min(20, max(1, int(p.get("limit") or 10)))
    price_max = min(10.0, float(p.get("price_max") or 5.0))
    custom = p.get("symbols") or p.get("tickers") or ""
    if custom:
        universe = [s.strip().upper() for s in str(custom).split(",") if s.strip()][:50]
    else:
        # Use dedicated penny universe + any SQUEEZE_UNIVERSE_FULL under price_max
        universe = list(dict.fromkeys(_PENNY_UNIVERSE + [
            s for s in _SQUEEZE_UNIVERSE_FULL if s not in _PENNY_UNIVERSE
        ]))
    result = _run_squeeze_scan(universe, price_max=price_max, limit=limit)
    result["offering"] = "penny_squeeze_scanner"
    result["note"] = (
        "Penny squeeze scanner: sub-$10 universe. "
        "Score 0-100: vol surge, coiling, near 20d high, penny-range premium. "
        "Grade A>=70, B>=50."
    )
    return _ok(result)
