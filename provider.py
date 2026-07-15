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
    """Fetch a URL and return parsed JSON."""
    if headers is None:
        headers = {"User-Agent": "scriptmasterlabs-acp/1.0", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=json.dumps(data).encode() if data else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:200]}
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
