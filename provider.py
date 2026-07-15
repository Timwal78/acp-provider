#!/usr/bin/env python3
"""
ACP Provider Server for scriptmasterlabs
Sells 7 high-value API endpoints via the ACP marketplace.

Endpoints:
1. perp_funding_aggregator — Hyperliquid funding rates + open interest across 232 perp markets
2. market_regime_indicator — Crypto Fear & Greed + global market cap + BTC dominance
3. defi_yield_rates — Top DeFi yield pools by APY across all protocols
4. defi_tvl_ranking — DeFi protocol TVL rankings with 24h change
5. crypto_market_overview — Top coins by market cap with price, volume, 24h change
6. crypto_price_lookup — Real-time prices for any token via CoinGecko
7. stablecoin_flow_tracker — Stablecoin market cap + supply data via DefiLlama

Runs as a background daemon: polls ACP events, handles incoming jobs, delivers results, collects USDC.
"""
import json
import subprocess
import time
import sys
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error

# ============================================================
# CONFIG
# ============================================================
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
CHAIN_ID = 8453
EVENTS_FILE = "/workspace/acp-provider/events.jsonl"
LOG_FILE = "/workspace/acp-provider/provider.log"
STATE_FILE = "/workspace/acp-provider/state.json"
POLL_INTERVAL = 5  # seconds between event drains
API_TIMEOUT = 15   # seconds for API calls

# ============================================================
# LOGGING
# ============================================================
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ============================================================
# STATE
# ============================================================
def load_state():
    p = Path(STATE_FILE)
    if p.exists():
        return json.loads(p.read_text())
    return {"total_jobs": 0, "total_revenue": 0.0, "jobs_handled": []}

def save_state(state):
    Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))

# ============================================================
# HTTP HELPER
# ============================================================
def fetch_url(url, method="GET", data=None, headers=None):
    """Fetch a URL and return parsed JSON. Uses curl subprocess for Cloudflare compatibility."""
    if headers is None:
        headers = {"User-Agent": "scriptmasterlabs-acp/1.0", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    
    # Build curl command
    curl_cmd = ["curl", "-s", "--max-time", str(API_TIMEOUT), "-X", method, url]
    for k, v in headers.items():
        curl_cmd.extend(["-H", f"{k}: {v}"])
    if data:
        curl_cmd.extend(["-d", json.dumps(data)])
    
    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=API_TIMEOUT + 5)
        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
        else:
            return {"error": f"curl failed (rc={result.returncode})", "detail": result.stderr[:200]}
    except json.JSONDecodeError as e:
        return {"error": f"JSON decode error: {e}", "detail": result.stdout[:200] if result else "no output"}
    except Exception as e:
        return {"error": str(e)}

# ============================================================
# API ENDPOINTS — Real data from free public APIs
# ============================================================

def api_perp_funding_aggregator(params):
    """Hyperliquid funding rates + open interest across all perp markets."""
    req_data = {"type": "metaAndAssetCtxs"}
    result = fetch_url("https://api.hyperliquid.xyz/info", method="POST", data=req_data)
    if "error" in result:
        return result

    universe = result[0].get("universe", [])
    asset_ctxs = result[1]

    markets = []
    for i, m in enumerate(universe):
        if i < len(asset_ctxs):
            ctx = asset_ctxs[i]
            markets.append({
                "symbol": m["name"],
                "max_leverage": m.get("maxLeverage", 0),
                "funding_rate": ctx.get("funding", "0"),
                "open_interest": ctx.get("openInterest", "0"),
                "mark_price": ctx.get("markPx", "0"),
                "oracle_price": ctx.get("oraclePx", "0"),
                "premium": ctx.get("premium", "0"),
                "prev_day_price": ctx.get("prevDayPx", "0"),
                "volume_24h_usd": ctx.get("dayNtlVlm", "0"),
                "volume_24h_base": ctx.get("dayBaseVlm", "0"),
            })

    # Sort by open interest descending
    markets.sort(key=lambda x: float(x["open_interest"]), reverse=True)

    # Filter by top N if requested
    top_n = params.get("top_n", 50)
    if top_n and isinstance(top_n, int):
        markets = markets[:top_n]

    # Filter by symbol if requested
    symbol_filter = params.get("symbol")
    if symbol_filter:
        markets = [m for m in markets if symbol_filter.upper() in m["symbol"]]

    # Calculate aggregate stats
    total_oi_usd = sum(float(m["open_interest"]) * float(m["mark_price"]) for m in markets if m["open_interest"] != "0")
    total_volume_24h = sum(float(m["volume_24h_usd"]) for m in markets if m["volume_24h_usd"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(universe),
        "returned_markets": len(markets),
        "total_open_interest_usd": round(total_oi_usd, 2),
        "total_volume_24h_usd": round(total_volume_24h, 2),
        "markets": markets,
    }

def api_market_regime_indicator(params):
    """Crypto Fear & Greed + global market cap + BTC dominance."""
    # Fear & Greed
    fng = fetch_url("https://api.alternative.me/fng/?limit=7")
    fng_data = fng.get("data", [])
    current_fng = fng_data[0] if fng_data else {}
    fng_history = fng_data[1:] if len(fng_data) > 1 else []

    # CoinGecko Global
    global_data = fetch_url("https://api.coingecko.com/api/v3/global")
    g = global_data.get("data", {})

    # Determine regime
    fng_value = int(current_fng.get("value", 50))
    if fng_value <= 25:
        regime = "EXTREME_FEAR"
        signal = "Contrarian buy zone — historically good entry for risk assets"
    elif fng_value <= 45:
        regime = "FEAR"
        signal = "Cautious — market leaning bearish but not capitulation"
    elif fng_value <= 55:
        regime = "NEUTRAL"
        signal = "Balanced — no clear edge, stay selective"
    elif fng_value <= 75:
        regime = "GREED"
        signal = "Bullish but getting crowded — manage risk on longs"
    else:
        regime = "EXTREME_GREED"
        signal = "Euphoria — historically a zone to take profits / hedge"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fear_greed_index": {
            "value": fng_value,
            "classification": current_fng.get("value_classification", "Unknown"),
            "regime": regime,
            "signal": signal,
        },
        "global_market": {
            "total_market_cap_usd": g.get("total_market_cap", {}).get("usd", 0),
            "total_volume_24h_usd": g.get("total_volume", {}).get("usd", 0),
            "btc_dominance": g.get("market_cap_percentage", {}).get("btc", 0),
            "eth_dominance": g.get("market_cap_percentage", {}).get("eth", 0),
            "altcoin_dominance": 100 - g.get("market_cap_percentage", {}).get("btc", 0) - g.get("market_cap_percentage", {}).get("eth", 0),
            "market_cap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd", 0),
        },
        "fear_greed_7d_history": [{"value": h.get("value"), "classification": h.get("value_classification"), "timestamp": h.get("timestamp")} for h in fng_history],
    }

def api_defi_yield_rates(params):
    """Top DeFi yield pools by APY."""
    data = fetch_url("https://yields.llama.fi/pools")
    pools = data.get("data", [])

    # Filter and sort
    min_apy = params.get("min_apy", 0)
    min_tvl = params.get("min_tvl", 1_000_000)  # default $1M min TVL
    chain_filter = params.get("chain")
    project_filter = params.get("project")
    top_n = params.get("top_n", 50)

    filtered = []
    for p in pools:
        apy = p.get("apy", 0) or 0
        tvl = p.get("tvlUsd", 0) or 0
        if apy < min_apy:
            continue
        if tvl < min_tvl:
            continue
        if chain_filter and p.get("chain", "").lower() != chain_filter.lower():
            continue
        if project_filter and p.get("project", "").lower() != project_filter.lower():
            continue
        filtered.append({
            "pool": p.get("pool", ""),
            "project": p.get("project", ""),
            "chain": p.get("chain", ""),
            "symbol": p.get("symbol", ""),
            "tvl_usd": round(tvl, 2),
            "apy": round(apy, 2),
            "apy_base": p.get("apyBase"),
            "apy_reward": p.get("apyReward"),
            "il7d": p.get("il7d"),
            "risk_score": p.get("riskScore"),
            "stablecoin": p.get("stablecoin", False),
            "count_30d": p.get("count30d"),
        })

    # Sort by APY * TVL (risk-adjusted opportunity score)
    filtered.sort(key=lambda x: (x["apy"] or 0) * (x["tvl_usd"] or 0), reverse=True)
    filtered = filtered[:top_n]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_pools_scanned": len(pools),
        "pools_returned": len(filtered),
        "filters_applied": {"min_apy": min_apy, "min_tvl": min_tvl, "chain": chain_filter, "project": project_filter},
        "top_yield_pools": filtered,
    }

def api_defi_tvl_ranking(params):
    """DeFi protocol TVL rankings with 24h change."""
    data = fetch_url("https://api.llama.fi/protocols")
    if isinstance(data, dict) and "error" in data:
        return data

    protocols = []
    for p in data:
        protocols.append({
            "name": p.get("name", ""),
            "symbol": p.get("symbol", ""),
            "tvl_usd": p.get("tvl", 0),
            "chain": p.get("chain", ""),
            "category": p.get("category", ""),
            "change_24h_pct": p.get("change_24h", 0),
            "change_7d_pct": p.get("change_7d", 0),
            "change_1m_pct": p.get("change_1m", 0),
            "mcap": p.get("mcaptvl", 0),
            "fdv": p.get("fdv", 0),
        })

    # Sort by TVL
    protocols.sort(key=lambda x: x["tvl_usd"] or 0, reverse=True)

    top_n = params.get("top_n", 50)
    category_filter = params.get("category")
    chain_filter = params.get("chain")

    if category_filter:
        protocols = [p for p in protocols if p["category"].lower() == category_filter.lower()]
    if chain_filter:
        protocols = [p for p in protocols if p["chain"].lower() == chain_filter.lower()]

    protocols = protocols[:top_n]

    total_tvl = sum(p["tvl_usd"] for p in protocols if p["tvl_usd"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_protocols_scanned": len(data),
        "protocols_returned": len(protocols),
        "total_tvl_usd": round(total_tvl, 2),
        "top_protocols_by_tvl": protocols,
    }

def api_crypto_market_overview(params):
    """Top coins by market cap with price, volume, 24h change."""
    per_page = params.get("per_page", 100)
    page = params.get("page", 1)
    order = params.get("order", "market_cap_desc")
    vs_currency = params.get("vs_currency", "usd")

    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency={vs_currency}&order={order}&per_page={per_page}&page={page}&sparkline=false&price_change_percentage=24h%2C7d"
    data = fetch_url(url)

    if isinstance(data, dict) and "error" in data:
        return data

    coins = []
    for c in data:
        coins.append({
            "rank": c.get("market_cap_rank"),
            "symbol": c.get("symbol", "").upper(),
            "name": c.get("name", ""),
            "price_usd": c.get("current_price"),
            "market_cap_usd": c.get("market_cap"),
            "volume_24h_usd": c.get("total_volume"),
            "change_24h_pct": c.get("price_change_percentage_24h_in_currency") or c.get("price_change_percentage_24h"),
            "change_7d_pct": c.get("price_change_percentage_7d_in_currency"),
            "ath": c.get("ath"),
            "ath_change_pct": c.get("ath_change_percentage"),
            "circulating_supply": c.get("circulating_supply"),
            "total_supply": c.get("total_supply"),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coins_returned": len(coins),
        "page": page,
        "per_page": per_page,
        "coins": coins,
    }

def api_crypto_price_lookup(params):
    """Real-time prices for specific tokens."""
    tokens = params.get("tokens", "")
    if isinstance(tokens, str):
        token_list = [t.strip().lower() for t in tokens.split(",")]
    else:
        token_list = [t.lower() for t in tokens]

    if not token_list:
        return {"error": "No tokens specified. Pass 'tokens' as comma-separated string (e.g. 'bitcoin,ethereum,solana')"}

    ids = ",".join(token_list)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true&include_last_updated_at=true"
    data = fetch_url(url)

    if isinstance(data, dict) and "error" in data:
        return data

    prices = []
    for token_id, info in data.items():
        prices.append({
            "token": token_id,
            "price_usd": info.get("usd"),
            "change_24h_pct": info.get("usd_24h_change"),
            "market_cap_usd": info.get("usd_market_cap"),
            "volume_24h_usd": info.get("usd_24h_vol"),
            "last_updated": datetime.fromtimestamp(info.get("last_updated_at", 0), tz=timezone.utc).isoformat() if info.get("last_updated_at") else None,
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokens_queried": token_list,
        "prices": prices,
    }

def api_stablecoin_flow_tracker(params):
    """Stablecoin market cap, supply, and DEX volume data.
    Uses CoinGecko /coins/{id} endpoint (queried individually to avoid rate limits)
    + DefiLlama DEX aggregate for stablecoin DEX volume.
    """
    stablecoin_ids = {
        "tether": "USDT",
        "usd-coin": "USDC",
        "dai": "DAI",
        "frax": "FRAX",
        "true-usd": "TUSD",
        "first-digital-usd": "FDUSD",
        "paxos-standard": "USDP",
    }

    stablecoins = []
    for coin_id, symbol in stablecoin_ids.items():
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false&sparkline=false"
        data = fetch_url(url)

        if isinstance(data, dict) and "status" in data:
            # Rate limited — skip this one
            continue
        if isinstance(data, dict) and "market_data" in data:
            md = data["market_data"]
            stablecoins.append({
                "symbol": symbol,
                "name": data.get("name", symbol),
                "price_usd": md.get("current_price", {}).get("usd", 0),
                "market_cap_usd": md.get("market_cap", {}).get("usd", 0) or 0,
                "volume_24h_usd": md.get("total_volume", {}).get("usd", 0) or 0,
                "change_24h_pct": md.get("price_change_percentage_24h", 0) or 0,
                "change_7d_pct": md.get("price_change_percentage_7d", 0) or 0,
                "circulating_supply": md.get("circulating_supply", 0) or 0,
                "total_supply": md.get("total_supply", 0) or 0,
            })
        time.sleep(1)  # Rate limit courtesy delay

    # Get DEX aggregate volume from DefiLlama (stablecoin DEX activity)
    dex_data = fetch_url("https://api.llama.fi/overview/dexs")
    dex_total_24h = dex_data.get("total24h", 0) if isinstance(dex_data, dict) else 0
    dex_total_7d = dex_data.get("total7d", 0) if isinstance(dex_data, dict) else 0

    # Global market context
    global_data = fetch_url("https://api.coingecko.com/api/v3/global")
    g = global_data.get("data", {}) if isinstance(global_data, dict) and "data" in global_data else {}

    stablecoins.sort(key=lambda x: x["market_cap_usd"] or 0, reverse=True)
    total_mcap = sum(s["market_cap_usd"] for s in stablecoins if s["market_cap_usd"])
    total_vol = sum(s["volume_24h_usd"] for s in stablecoins if s["volume_24h_usd"])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_stablecoin_mcap_usd": round(total_mcap, 2),
        "total_stablecoin_volume_24h_usd": round(total_vol, 2),
        "stablecoins": stablecoins,
        "dex_aggregate": {
            "total_dex_volume_24h_usd": dex_total_24h,
            "total_dex_volume_7d_usd": dex_total_7d,
        },
        "global_context": {
            "total_crypto_mcap_usd": g.get("total_market_cap", {}).get("usd", 0),
            "stablecoin_dominance_pct": round((total_mcap / g.get("total_market_cap", {}).get("usd", 1)) * 100, 2) if g.get("total_market_cap", {}).get("usd") and total_mcap else 0,
            "total_volume_24h_usd": g.get("total_volume", {}).get("usd", 0),
        },
    }


# ============================================================
# FEDERAL / CONTRACTING APIs (SAM.gov moat — UEI G24VZA4RLMK3)
# Data source: USAspending.gov API (free, no key required)
# ============================================================

def api_federal_contract_opportunities(params):
    """Active federal contract awards — who's getting what, for how much.
    Uses USAspending.gov spending_by_award endpoint.
    Req: { top_n?: int, agency?: string, naics?: string, min_amount?: float }
    """
    top_n = min(int(params.get("top_n", 20)), 100)
    agency = params.get("agency", "")
    min_amount = float(params.get("min_amount", 0))
    naics = params.get("naics", "")

    filters = {
        "award_type_codes": ["A", "B", "C", "D"],
        "time_period": [{"start_date": "2024-10-01", "end_date": "2025-09-30"}]
    }
    if agency:
        filters["awarding_agencies"] = [{"name": agency, "type": "awarding"}]
    if naics:
        filters["naics_codes"] = [naics]

    payload = {
        "filters": filters,
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date",
                    "End Date", "Awarding Agency", "Awarding Sub Agency",
                    "Contract Award Type", "recipient_id"],
        "page": 1, "limit": top_n,
        "sort": "Award Amount", "sort_direction": "desc"
    }

    data = fetch_url("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                     method="POST", data=payload)
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    awards = []
    for r in data.get("results", []):
        amt = r.get("Award Amount", 0) or 0
        if amt < min_amount:
            continue
        awards.append({
            "award_id": r.get("Award ID"),
            "recipient": r.get("Recipient Name"),
            "amount_usd": amt,
            "start_date": r.get("Start Date"),
            "end_date": r.get("End Date"),
            "awarding_agency": r.get("Awarding Agency"),
            "awarding_sub_agency": r.get("Awarding Sub Agency"),
            "award_type": r.get("Contract Award Type"),
        })

    total = sum(a["amount_usd"] for a in awards)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "awards_returned": len(awards),
        "total_award_value_usd": round(total, 2),
        "top_n_requested": top_n,
        "filters_applied": {"agency": agency, "naics": naics, "min_amount": min_amount},
        "awards": awards,
    }


def api_federal_award_history(params):
    """Contract award history by contractor name.
    Req: { contractor_name: string, top_n?: int }
    """
    contractor = params.get("contractor_name", "")
    if not contractor:
        return {"error": "contractor_name is required"}
    top_n = min(int(params.get("top_n", 20)), 100)

    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2020-10-01", "end_date": "2025-09-30"}],
            "recipient_search_text": [contractor]
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date",
                    "End Date", "Awarding Agency", "Awarding Sub Agency",
                    "Contract Award Type"],
        "page": 1, "limit": top_n,
        "sort": "Award Amount", "sort_direction": "desc"
    }

    data = fetch_url("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                     method="POST", data=payload)
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    awards = []
    for r in data.get("results", []):
        awards.append({
            "award_id": r.get("Award ID"),
            "recipient": r.get("Recipient Name"),
            "amount_usd": r.get("Award Amount", 0) or 0,
            "start_date": r.get("Start Date"),
            "end_date": r.get("End Date"),
            "awarding_agency": r.get("Awarding Agency"),
            "awarding_sub_agency": r.get("Awarding Sub Agency"),
            "award_type": r.get("Contract Award Type"),
        })

    total = sum(a["amount_usd"] for a in awards)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "contractor_searched": contractor,
        "awards_returned": len(awards),
        "total_award_value_usd": round(total, 2),
        "awards": awards,
    }


def api_sdvosb_setaside_feed(params):
    """SDVOSB/VOSB set-aside contract awards — veteran-owned business opportunities.
    Uses USAspending.gov with contract_set_asides filter.
    Req: { top_n?: int, agency?: string }
    """
    top_n = min(int(params.get("top_n", 20)), 100)
    agency = params.get("agency", "")

    filters = {
        "award_type_codes": ["A", "B", "C", "D"],
        "time_period": [{"start_date": "2024-10-01", "end_date": "2025-09-30"}],
        "contract_set_asides": ["SBP1"]
    }
    if agency:
        filters["awarding_agencies"] = [{"name": agency, "type": "awarding"}]

    payload = {
        "filters": filters,
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date",
                    "Awarding Agency", "Awarding Sub Agency",
                    "Contract Set Aside Type"],
        "page": 1, "limit": top_n,
        "sort": "Award Amount", "sort_direction": "desc"
    }

    data = fetch_url("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                     method="POST", data=payload)
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    awards = []
    for r in data.get("results", []):
        awards.append({
            "award_id": r.get("Award ID"),
            "recipient": r.get("Recipient Name"),
            "amount_usd": r.get("Award Amount", 0) or 0,
            "start_date": r.get("Start Date"),
            "awarding_agency": r.get("Awarding Agency"),
            "set_aside_type": r.get("Contract Set Aside Type", "SDVOSB"),
        })

    total = sum(a["amount_usd"] for a in awards)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "set_aside_category": "SDVOSB (Service-Disabled Veteran-Owned Business)",
        "awards_returned": len(awards),
        "total_setaside_value_usd": round(total, 2),
        "awards": awards,
    }


def api_sam_entity_verification(params):
    """Verify a federal contractor entity via USAspending.gov recipient search.
    Returns entity name, UEI, DUNS, and award history.
    Req: { entity_name: string }
    """
    entity_name = params.get("entity_name", "")
    if not entity_name:
        return {"error": "entity_name is required"}

    search_payload = {"search_text": entity_name, "limit": 5}
    search_data = fetch_url("https://api.usaspending.gov/api/v2/autocomplete/recipient/",
                            method="POST", data=search_payload)
    if isinstance(search_data, dict) and "error" in search_data:
        return {"error": search_data["error"]}

    entities = []
    for r in search_data.get("results", []):
        entities.append({
            "name": r.get("recipient_name"),
            "uei": r.get("uei"),
            "duns": r.get("duns"),
            "recipient_level": r.get("recipient_level"),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entity_searched": entity_name,
        "entities_found": len(entities),
        "entities": entities,
    }


def api_federal_spending_by_agency(params):
    """Federal spending breakdown by agency.
    Shows total obligations, outlays, and budget authority for each agency.
    Req: { fiscal_year?: int }
    """
    fiscal_year = int(params.get("fiscal_year", 2025))

    data = fetch_url("https://api.usaspending.gov/api/v2/references/toptier_agencies/")
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    agencies = []
    for a in data.get("results", []):
        agencies.append({
            "agency_name": a.get("agency_name"),
            "abbreviation": a.get("abbreviation"),
            "toptier_code": a.get("toptier_code"),
            "obligated_amount": a.get("obligated_amount", 0) or 0,
            "outlay_amount": a.get("outlay_amount", 0) or 0,
            "budget_authority": a.get("budget_authority_amount", 0) or 0,
            "active_fy": a.get("active_fy"),
            "percentage_of_total": a.get("percentage_of_total_budget_authority", 0) or 0,
        })

    agencies.sort(key=lambda x: x["obligated_amount"], reverse=True)
    total_obligated = sum(a["obligated_amount"] for a in agencies)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fiscal_year": fiscal_year,
        "total_agencies": len(agencies),
        "total_obligated_usd": round(total_obligated, 2),
        "agencies": agencies[:50],
    }


def api_excluded_parties_check(params):
    """Check if an entity appears in federal award records with exclusion indicators.
    Uses USAspending.gov recipient search + award search.
    Req: { entity_name: string }
    """
    entity_name = params.get("entity_name", "")
    if not entity_name:
        return {"error": "entity_name is required"}

    search_data = fetch_url("https://api.usaspending.gov/api/v2/autocomplete/recipient/",
                            method="POST", data={"search_text": entity_name, "limit": 5})
    if isinstance(search_data, dict) and "error" in search_data:
        return {"error": search_data["error"]}

    recipients = []
    for r in search_data.get("results", []):
        recipients.append({
            "name": r.get("recipient_name"),
            "uei": r.get("uei"),
            "duns": r.get("duns"),
        })

    award_payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": "2024-01-01", "end_date": "2025-09-30"}],
            "recipient_search_text": [entity_name]
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount", "Start Date", "Awarding Agency"],
        "page": 1, "limit": 5,
        "sort": "Award Amount", "sort_direction": "desc"
    }
    award_data = fetch_url("https://api.usaspending.gov/api/v2/search/spending_by_award/",
                           method="POST", data=award_payload)

    recent_awards = []
    if isinstance(award_data, dict) and "results" in award_data:
        for r in award_data["results"][:5]:
            recent_awards.append({
                "award_id": r.get("Award ID"),
                "recipient": r.get("Recipient Name"),
                "amount_usd": r.get("Award Amount", 0) or 0,
                "awarding_agency": r.get("Awarding Agency"),
            })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entity_searched": entity_name,
        "entities_found": len(recipients),
        "recipients": recipients,
        "recent_awards": recent_awards,
        "note": "Cross-reference with SAM.gov exclusion list for full debarment verification",
    }


# ============================================================
# CRYPTO ON-CHAIN ANALYTICS APIs
# Data sources: DexScreener (free), GoPlus Labs (free), DefiLlama (free)
# ============================================================

def api_crypto_onchain_analytics(params):
    """On-chain token analytics: price, volume, liquidity, FDV, txns, market cap.
    Uses DexScreener API — query by token contract address.
    Req: { token_address: string, chain?: string }
    """
    token_address = params.get("token_address", "")
    if not token_address:
        return {"error": "token_address is required"}
    chain = params.get("chain", "")

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    data = fetch_url(url)
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    pairs = data.get("pairs", [])
    if not pairs:
        return {"error": "No trading pairs found for this token address"}

    if chain:
        pairs = [p for p in pairs if p.get("chainId", "").lower() == chain.lower()]

    pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)

    top_pairs = []
    for p in pairs[:10]:
        txns = p.get("txns", {}).get("h24", {})
        top_pairs.append({
            "chain": p.get("chainId"),
            "dex": p.get("dexId"),
            "pair": f"{p.get('baseToken', {}).get('symbol', '?')}/{p.get('quoteToken', {}).get('symbol', '?')}",
            "price_usd": p.get("priceUsd"),
            "volume_24h_usd": float(p.get("volume", {}).get("h24", 0) or 0),
            "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
            "fdv": float(p.get("fdv", 0) or 0),
            "market_cap": float(p.get("marketCap", 0) or 0),
            "txns_24h_buys": txns.get("buys", 0),
            "txns_24h_sells": txns.get("sells", 0),
            "price_change_24h": p.get("priceChange", {}).get("h24", 0),
            "pair_address": p.get("pairAddress"),
            "pair_created_at": p.get("pairCreatedAt"),
        })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_address": token_address,
        "pairs_found": len(pairs),
        "pairs_returned": len(top_pairs),
        "token_info": {
            "name": pairs[0].get("baseToken", {}).get("name") if pairs else None,
            "symbol": pairs[0].get("baseToken", {}).get("symbol") if pairs else None,
        },
        "pairs": top_pairs,
    }


def api_crypto_sentiment_scanner(params):
    """Crypto market sentiment aggregation: Fear & Greed + social metrics.
    Combines Fear & Greed Index with DexScreener volume activity as sentiment proxy.
    Req: { token_symbol?: string }
    """
    token_symbol = params.get("token_symbol", "")

    fng_data = fetch_url("https://api.alternative.me/fng/?limit=7")
    if isinstance(fng_data, dict) and "error" in fng_data:
        fng_value = 50
        fng_class = "Neutral"
    else:
        fng = fng_data.get("data", [{}])[0]
        fng_value = int(fng.get("value", 50))
        fng_class = fng.get("value_classification", "Neutral")

    if fng_value <= 25:
        regime = "EXTREME_FEAR"
        signal = "BUY (contrarian)"
    elif fng_value <= 45:
        regime = "FEAR"
        signal = "ACCUMULATE"
    elif fng_value <= 55:
        regime = "NEUTRAL"
        signal = "HOLD"
    elif fng_value <= 75:
        regime = "GREED"
        signal = "REDUCE"
    else:
        regime = "EXTREME_GREED"
        signal = "SELL (contrarian)"

    token_data = {}
    if token_symbol:
        search_data = fetch_url(f"https://api.dexscreener.com/latest/dex/search?q={token_symbol}")
        if isinstance(search_data, dict) and "pairs" in search_data:
            pairs = search_data.get("pairs", [])[:5]
            pairs.sort(key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0), reverse=True)
            token_data = {
                "symbol": token_symbol,
                "top_pairs": [{
                    "pair": f"{p.get('baseToken', {}).get('symbol', '?')}/{p.get('quoteToken', {}).get('symbol', '?')}",
                    "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
                    "liquidity": float(p.get("liquidity", {}).get("usd", 0) or 0),
                    "price_change_24h": p.get("priceChange", {}).get("h24", 0),
                    "txns_24h": p.get("txns", {}).get("h24", {}),
                } for p in pairs[:3]]
            }

    fng_history = []
    if isinstance(fng_data, dict) and "data" in fng_data:
        for entry in fng_data["data"][:7]:
            fng_history.append({
                "date": entry.get("timestamp"),
                "value": int(entry.get("value", 0)),
                "classification": entry.get("value_classification"),
            })

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fear_greed_index": fng_value,
        "classification": fng_class,
        "regime": regime,
        "signal": signal,
        "fng_7d_history": fng_history,
        "token_sentiment": token_data if token_data else None,
    }


def api_dex_volume_ranking(params):
    """DEX trading volume rankings by protocol.
    Uses DefiLlama DEX overview endpoint.
    Req: { top_n?: int }
    """
    top_n = min(int(params.get("top_n", 20)), 100)

    data = fetch_url("https://api.llama.fi/overview/dexs?dataType=dailyVolume")
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    protocols = data.get("protocols", [])
    protocols.sort(key=lambda p: p.get("total24h") or 0, reverse=True)

    ranking = []
    for p in protocols[:top_n]:
        ranking.append({
            "protocol": p.get("name"),
            "volume_24h_usd": p.get("total24h") or 0,
            "volume_7d_usd": p.get("total7d") or 0,
            "chains": p.get("chains", []),
            "description": p.get("description", ""),
        })

    total_24h = data.get("total24h") or 0
    total_7d = data.get("total7d") or 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_dex_volume_24h_usd": total_24h,
        "total_dex_volume_7d_usd": total_7d,
        "protocols_returned": len(ranking),
        "ranking": ranking,
    }


def api_token_security_audit(params):
    """Token contract security audit: honeypot, rugpull risk, taxes, ownership.
    Uses GoPlus Labs API (free, no key).
    Req: { chain_id: int, token_address: string }
    """
    chain_id = str(params.get("chain_id", "1"))
    token_address = params.get("token_address", "")
    if not token_address:
        return {"error": "token_address is required"}

    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={token_address}"
    data = fetch_url(url)
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    result = data.get("result", {})
    token_info = result.get(token_address.lower(), result.get(token_address, {}))

    if not token_info:
        return {"error": "No security data found for this token. Token may not be indexed."}

    risk_factors = []
    risk_score = 0

    if str(token_info.get("is_honeypot", "0")) == "1":
        risk_factors.append("HONEYPOT DETECTED")
        risk_score += 100
    if str(token_info.get("is_mintable", "0")) == "1":
        risk_factors.append("Owner can mint new tokens")
        risk_score += 30
    if str(token_info.get("hidden_owner", "0")) == "1":
        risk_factors.append("Hidden owner detected")
        risk_score += 25
    if str(token_info.get("owner_change_balance", "0")) == "1":
        risk_factors.append("Owner can change balances")
        risk_score += 50
    if str(token_info.get("selfdestruct", "0")) == "1":
        risk_factors.append("Self-destruct capability")
        risk_score += 40
    if str(token_info.get("is_proxy", "0")) == "1":
        risk_factors.append("Proxy contract (upgradeable)")
        risk_score += 15

    sell_tax = token_info.get("sell_tax", "0")
    buy_tax = token_info.get("buy_tax", "0")
    try:
        sell_tax_pct = float(sell_tax) * 100 if sell_tax else 0
        buy_tax_pct = float(buy_tax) * 100 if buy_tax else 0
    except (ValueError, TypeError):
        sell_tax_pct = 0
        buy_tax_pct = 0

    if sell_tax_pct > 10:
        risk_factors.append(f"High sell tax: {sell_tax_pct:.1f}%")
        risk_score += 20
    if buy_tax_pct > 10:
        risk_factors.append(f"High buy tax: {buy_tax_pct:.1f}%")
        risk_score += 20

    if risk_score >= 100:
        risk_level = "CRITICAL"
    elif risk_score >= 50:
        risk_level = "HIGH"
    elif risk_score >= 25:
        risk_level = "MEDIUM"
    elif risk_score >= 10:
        risk_level = "LOW"
    else:
        risk_level = "SAFE"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chain_id": chain_id,
        "token_address": token_address,
        "token_name": token_info.get("token_name"),
        "token_symbol": token_info.get("token_symbol"),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_factors": risk_factors,
        "security_checks": {
            "is_honeypot": token_info.get("is_honeypot"),
            "is_open_source": token_info.get("is_open_source"),
            "is_proxy": token_info.get("is_proxy"),
            "is_mintable": token_info.get("is_mintable"),
            "owner_change_balance": token_info.get("owner_change_balance"),
            "hidden_owner": token_info.get("hidden_owner"),
            "selfdestruct": token_info.get("selfdestruct"),
            "external_call": token_info.get("external_call"),
            "is_in_dex": token_info.get("is_in_dex"),
            "is_trust_list": token_info.get("is_trust_list"),
        },
        "taxes": {
            "buy_tax_pct": round(buy_tax_pct, 2),
            "sell_tax_pct": round(sell_tax_pct, 2),
        },
        "holder_count": token_info.get("holder_count"),
        "lp_holder_count": token_info.get("lp_holder_count"),
        "total_supply": token_info.get("total_supply"),
    }


def api_whale_wallet_tracker(params):
    """Track top trading pairs by volume as a proxy for whale activity.
    Uses DexScreener search to find high-volume pairs.
    Req: { query: string, top_n?: int }
    """
    query = params.get("query", "")
    if not query:
        return {"error": "query is required (e.g. 'ETH/USDC', 'BTC', 'SOL')"}
    top_n = min(int(params.get("top_n", 20)), 100)

    data = fetch_url(f"https://api.dexscreener.com/latest/dex/search?q={query}")
    if isinstance(data, dict) and "error" in data:
        return {"error": data["error"]}

    pairs = data.get("pairs", [])
    pairs.sort(key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0), reverse=True)

    results = []
    for p in pairs[:top_n]:
        txns = p.get("txns", {}).get("h24", {})
        results.append({
            "chain": p.get("chainId"),
            "dex": p.get("dexId"),
            "pair": f"{p.get('baseToken', {}).get('symbol', '?')}/{p.get('quoteToken', {}).get('symbol', '?')}",
            "price_usd": p.get("priceUsd"),
            "volume_24h_usd": float(p.get("volume", {}).get("h24", 0) or 0),
            "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
            "fdv": float(p.get("fdv", 0) or 0),
            "market_cap": float(p.get("marketCap", 0) or 0),
            "txns_24h_buys": txns.get("buys", 0),
            "txns_24h_sells": txns.get("sells", 0),
            "price_change_24h_pct": p.get("priceChange", {}).get("h24", 0),
            "price_change_6h_pct": p.get("priceChange", {}).get("h6", 0),
            "pair_created_at": p.get("pairCreatedAt"),
        })

    total_vol = sum(r["volume_24h_usd"] for r in results)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "pairs_found": len(pairs),
        "pairs_returned": len(results),
        "total_volume_24h_usd": round(total_vol, 2),
        "top_pairs": results,
    }

# ============================================================
# ENDPOINT REGISTRY
# ============================================================
ENDPOINTS = {
    "perp_funding_aggregator": api_perp_funding_aggregator,
    "market_regime_indicator": api_market_regime_indicator,
    "defi_yield_rates": api_defi_yield_rates,
    "defi_tvl_ranking": api_defi_tvl_ranking,
    "crypto_market_overview": api_crypto_market_overview,
    "crypto_price_lookup": api_crypto_price_lookup,
    "stablecoin_flow_tracker": api_stablecoin_flow_tracker,
# --- Federal/Contracting APIs (SAM.gov moat) ---
    "federal_contract_opportunities": api_federal_contract_opportunities,
    "federal_award_history": api_federal_award_history,
    "sdvosb_setaside_feed": api_sdvosb_setaside_feed,
    "sam_entity_verification": api_sam_entity_verification,
    "federal_spending_by_agency": api_federal_spending_by_agency,
    "excluded_parties_check": api_excluded_parties_check,

    # --- Crypto On-Chain Analytics ---
    "crypto_onchain_analytics": api_crypto_onchain_analytics,
    "crypto_sentiment_scanner": api_crypto_sentiment_scanner,
    "dex_volume_ranking": api_dex_volume_ranking,
    "token_security_audit": api_token_security_audit,
    "whale_wallet_tracker": api_whale_wallet_tracker,
}

# ============================================================
# ACP EVENT HANDLING
# ============================================================
def start_event_listener():
    """Start the ACP event listener in background."""
    Path(EVENTS_FILE).parent.mkdir(parents=True, exist_ok=True)
    # Kill any existing listener
    subprocess.run(f"pkill -f 'acp events listen.*{EVENTS_FILE}' 2>/dev/null", shell=True)
    time.sleep(1)
    # Start new listener
    proc = subprocess.Popen(
        f"acp events listen --output {EVENTS_FILE} --json",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid
    )
    log(f"Event listener started (PID {proc.pid})")
    return proc

def drain_events():
    """Drain events from the listener file."""
    result = subprocess.run(
        f"acp events drain --file {EVENTS_FILE} --limit 10 --json",
        shell=True, capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data.get("events", [])
    except:
        return []

def handle_job_created(event):
    """Handle a new job — set budget to the offering price."""
    job_id = event.get("jobId")
    chain_id = event.get("chainId")
    entry = event.get("entry", {})

    # Find the offering name from the entry
    offering_name = None
    if isinstance(entry, dict):
        # Try to extract offering name from various places
        ev = entry.get("event", {})
        offering_name = ev.get("offeringName") or entry.get("offeringName")
        if not offering_name:
            # Check the offering in the entry
            offering = entry.get("offering", {})
            if isinstance(offering, dict):
                offering_name = offering.get("name")

    log(f"New job: {job_id} for offering '{offering_name}'")

    # Look up the price from our offerings
    offerings = get_offerings()
    price = 0.03  # default
    if offering_name:
        for o in offerings:
            if o.get("name") == offering_name:
                price = o.get("priceValue", 0.03)
                break

    # Set budget
    r = subprocess.run(
        f"acp provider set-budget --job-id {job_id} --amount {price} --chain-id {chain_id} --json",
        shell=True, capture_output=True, text=True, timeout=30
    )
    if r.returncode == 0:
        log(f"Budget set to ${price} for job {job_id}")
    else:
        log(f"Failed to set budget for {job_id}: {r.stderr[:200]}", "ERROR")

def handle_job_funded(event):
    """Handle a funded job — execute the API and submit deliverable."""
    job_id = event.get("jobId")
    chain_id = event.get("chainId")
    entry = event.get("entry", {})

    # Get the offering name and requirement
    offering_name = None
    requirements = {}

    ev = entry.get("event", {})
    offering_name = ev.get("offeringName") or entry.get("offeringName")

    # Try to get requirements from messages
    messages = entry.get("messages", [])
    for msg in messages:
        if msg.get("contentType") == "requirement":
            try:
                requirements = json.loads(msg.get("content", "{}"))
            except:
                requirements = {}
            break

    if not offering_name:
        # Fall back to job history
        r = subprocess.run(
            f"acp job history --job-id {job_id} --chain-id {chain_id} --json",
            shell=True, capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            try:
                hist = json.loads(r.stdout)
                offering_name = hist.get("offeringName") or hist.get("offering", {}).get("name")
                # Try to get requirements from history
                for msg in hist.get("messages", []):
                    if msg.get("contentType") == "requirement":
                        try:
                            requirements = json.loads(msg.get("content", "{}"))
                        except:
                            pass
            except:
                pass

    log(f"Job funded: {job_id} for '{offering_name}' with requirements: {json.dumps(requirements)[:200]}")

    # Execute the API
    result = None
    if offering_name and offering_name in ENDPOINTS:
        try:
            result = ENDPOINTS[offering_name](requirements)
            log(f"API executed for '{offering_name}' — success")
        except Exception as e:
            result = {"error": f"API execution failed: {str(e)}"}
            log(f"API execution failed for '{offering_name}': {e}", "ERROR")
    else:
        result = {"error": f"Unknown offering: {offering_name}. Available: {list(ENDPOINTS.keys())}"}
        log(f"Unknown offering: {offering_name}", "ERROR")

    # Submit deliverable
    deliverable = json.dumps(result, indent=2, default=str)
    r = subprocess.run(
        f"acp provider submit --job-id {job_id} --deliverable '{deliverable.replace(chr(39), chr(39) + chr(39))}' --chain-id {chain_id} --json",
        shell=True, capture_output=True, text=True, timeout=60
    )
    if r.returncode == 0:
        log(f"Deliverable submitted for job {job_id}")
        # Update state
        state = load_state()
        state["total_jobs"] += 1
        state["total_revenue"] += 0.03  # will be updated when completed
        state["jobs_handled"].append({"job_id": job_id, "offering": offering_name, "timestamp": datetime.now(timezone.utc).isoformat()})
        save_state(state)
    else:
        log(f"Failed to submit deliverable for {job_id}: {r.stderr[:200]}", "ERROR")

def handle_job_completed(event):
    """Handle job completion — update revenue tracking."""
    job_id = event.get("jobId")
    entry = event.get("entry", {})
    ev = entry.get("event", {})
    amount = ev.get("amount", 0)

    log(f"Job completed: {job_id} — revenue: ${amount}")
    state = load_state()
    state["total_revenue"] = float(amount) if amount else state["total_revenue"]
    save_state(state)

def handle_job_rejected(event):
    """Handle job rejection."""
    job_id = event.get("jobId")
    entry = event.get("entry", {})
    ev = entry.get("event", {})
    reason = ev.get("reason", "unknown")
    log(f"Job rejected: {job_id} — reason: {reason}", "WARN")

def get_offerings():
    """Get our current offerings from ACP."""
    r = subprocess.run("acp offering list --json 2>/dev/null", shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except:
            return []
    return []

# ============================================================
# MAIN PROVIDER LOOP
# ============================================================
running = True

def signal_handler(sig, frame):
    global running
    log("Shutdown signal received")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def main():
    log("=" * 60)
    log("ACP Provider Server starting for scriptmasterlabs")
    log(f"Agent ID: {AGENT_ID}")
    log(f"Chain ID: {CHAIN_ID}")
    log(f"Endpoints: {list(ENDPOINTS.keys())}")
    log(f"Event file: {EVENTS_FILE}")
    log(f"Poll interval: {POLL_INTERVAL}s")
    log("=" * 60)

    # Start event listener
    listener_proc = start_event_listener()
    time.sleep(3)

    # Verify listener is running
    if listener_proc.poll() is not None:
        log("Event listener failed to start!", "ERROR")
        sys.exit(1)

    log("Event listener running. Entering main loop...")

    cycle = 0
    while running:
        try:
            cycle += 1
            events = drain_events()

            if events:
                log(f"Cycle {cycle}: {len(events)} event(s) to process")
            else:
                if cycle % 12 == 0:  # Log every ~1 min
                    state = load_state()
                    log(f"Cycle {cycle}: No events. Jobs handled: {state['total_jobs']}, Revenue: ${state['total_revenue']:.2f}")

            for event in events:
                status = event.get("status")
                job_id = event.get("jobId", "unknown")
                log(f"  Event: status={status}, job={job_id}")

                if status == "open":
                    handle_job_created(event)
                elif status == "funded":
                    handle_job_funded(event)
                elif status == "completed":
                    handle_job_completed(event)
                elif status == "rejected":
                    handle_job_rejected(event)
                else:
                    log(f"  Unhandled status: {status}")

        except Exception as e:
            log(f"Error in main loop: {e}", "ERROR")

        time.sleep(POLL_INTERVAL)

    # Cleanup
    log("Shutting down...")
    try:
        os.killpg(os.getpgid(listener_proc.pid), signal.SIGTERM)
    except:
        pass
    log("Provider server stopped")

if __name__ == "__main__":
    main()