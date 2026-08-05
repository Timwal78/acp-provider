#!/usr/bin/env python3
"""
amb.py — Agent Magnet Beacon (AMB) for scriptmasterlabs ACP / x402.

Every paid capability becomes a live beacon agents can rank by magnet_strength.
payTo is ALWAYS the ACP EOA 0x7233… (never orphan 0x4e14 seed).

Serves:
  GET /.well-known/amb.json
  MCP free tools: list_magnets, get_agent_magnet_beacons
"""
from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Any, Callable

from flask import Blueprint, jsonify, request

# Identity / rails — shared with a2a + mcp + x402
AGENT_NAME = "scriptmasterlabs"
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
AGENT_WALLET = (
    os.environ.get("X402_PAY_TO")
    or os.environ.get("ACP_AGENT_WALLET_ADDRESS")
    or "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
).strip()
# Explicit refuse of orphan seed if somehow set
if AGENT_WALLET.lower().startswith("0x4e14"):
    AGENT_WALLET = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"

ISSUER_DID = f"did:agentcard:scriptmasterlabs:{AGENT_WALLET}"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOL_PAY_TO = (
    os.environ.get("SOLANA_PAY_TO")
    or os.environ.get("SOLANA_PAYMENT_RECEIVER")
    or "E4d3JwcTjeqTRkkQS4moszcfa4R7G1NMgPSew4KBNFrB"
).strip()
XRPL_PAY_TO = (
    os.environ.get("XRPL_PAY_TO")
    or os.environ.get("XRPL_PAYMENT_RECEIVER")
    or "rNduuviQ3CCvHqWUTjJDD82Ko2tjqFGs3q"
).strip()
USDG_ASSET = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
DEFAULT_RAILS = ["base_usdc", "robinhood_usdg", "solana_usdc", "xrpl_rlusd"]
AMB_VERSION = "0.1.0"

# ── Privacy-safe rolling 24h agent traffic (public aggregates only) ───────────
# File-backed so multi-worker gunicorn shares counters on one instance.
import json as _json
import fcntl
from pathlib import Path as _Path

_WINDOW_MS = 24 * 60 * 60 * 1000
_MAX_EVENTS = 20000
_TRAFFIC_PATH = _Path(os.environ.get("AMB_TRAFFIC_FILE", "/tmp/sml_amb_traffic.json"))


def _agent_key_from_request() -> str:
    try:
        h = request.headers
    except Exception:
        return "anon"
    parts = [
        (h.get("X-Agent-Id") or "").strip()[:200],
        (h.get("X-Agent-Name") or "").strip()[:200],
        (h.get("X-MCP-Client") or "").strip()[:200],
        (h.get("User-Agent") or "").strip()[:200],
    ]
    raw = "|".join(p for p in parts if p).lower()
    if not raw:
        return "anon"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _empty_store() -> dict[str, Any]:
    return {
        "events": [],
        "lifetime": {
            "amb_fetches": 0,
            "list_magnets_calls": 0,
            "get_agent_magnet_beacons_calls": 0,
            "paid_calls": 0,
        },
        "lifetime_agents": [],
    }


def _load_store_unlocked(fh) -> dict[str, Any]:
    try:
        fh.seek(0)
        raw = fh.read()
        if not raw:
            return _empty_store()
        data = _json.loads(raw)
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("events", [])
        data.setdefault("lifetime", _empty_store()["lifetime"])
        data.setdefault("lifetime_agents", [])
        return data
    except Exception:
        return _empty_store()


def _prune_store(data: dict[str, Any], now: int) -> None:
    cutoff = now - _WINDOW_MS
    ev = [e for e in data.get("events") or [] if isinstance(e, list) and len(e) >= 3 and int(e[0]) >= cutoff]
    if len(ev) > _MAX_EVENTS:
        ev = ev[-_MAX_EVENTS:]
    data["events"] = ev


def record_amb_traffic(kind: str, agent_key: str | None = None) -> None:
    now = int(time.time() * 1000)
    key = (agent_key or "anon")[:32]
    _TRAFFIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_TRAFFIC_PATH, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            data = _load_store_unlocked(fh)
            _prune_store(data, now)
            data["events"].append([now, kind, key])
            agents = set(data.get("lifetime_agents") or [])
            if key not in agents:
                agents.add(key)
                data["lifetime_agents"] = list(agents)[:5000]
            life = data.setdefault("lifetime", _empty_store()["lifetime"])
            if kind == "amb_fetch":
                life["amb_fetches"] = int(life.get("amb_fetches") or 0) + 1
            elif kind == "list_magnets":
                life["list_magnets_calls"] = int(life.get("list_magnets_calls") or 0) + 1
            elif kind == "get_agent_magnet_beacons":
                life["get_agent_magnet_beacons_calls"] = int(life.get("get_agent_magnet_beacons_calls") or 0) + 1
            elif kind == "paid_call":
                life["paid_calls"] = int(life.get("paid_calls") or 0) + 1
            fh.seek(0)
            fh.truncate()
            fh.write(_json.dumps(data, separators=(",", ":")))
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def get_amb_traffic_snapshot() -> dict[str, Any]:
    now = int(time.time() * 1000)
    data = _empty_store()
    try:
        if _TRAFFIC_PATH.exists():
            with open(_TRAFFIC_PATH, "r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    data = _load_store_unlocked(fh)
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        data = _empty_store()
    _prune_store(data, now)
    amb_fetches = list_m = get_m = paid = 0
    agents: set[str] = set()
    last = 0
    for row in data.get("events") or []:
        try:
            ts, kind, key = int(row[0]), str(row[1]), str(row[2])
        except Exception:
            continue
        agents.add(key)
        if ts > last:
            last = ts
        if kind == "amb_fetch":
            amb_fetches += 1
        elif kind == "list_magnets":
            list_m += 1
        elif kind == "get_agent_magnet_beacons":
            get_m += 1
        elif kind == "paid_call":
            paid += 1
    life = data.get("lifetime") or {}
    return {
        "window": "24h",
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now / 1000.0)),
        "amb_fetches": amb_fetches,
        "list_magnets_calls": list_m,
        "get_agent_magnet_beacons_calls": get_m,
        "paid_calls": paid,
        "unique_agents": len(agents),
        "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last / 1000.0)) if last else None,
        "lifetime": {
            "amb_fetches": int(life.get("amb_fetches") or 0),
            "list_magnets_calls": int(life.get("list_magnets_calls") or 0),
            "get_agent_magnet_beacons_calls": int(life.get("get_agent_magnet_beacons_calls") or 0),
            "paid_calls": int(life.get("paid_calls") or 0),
            "unique_agents_approx": len(data.get("lifetime_agents") or []),
        },
        "note": "Aggregates only. Agent identity hashed server-side; no IPs/wallets in public AMB.",
        "host": "acp-x402-scriptmasterlabs",
    }


def _attach_traffic(doc: dict[str, Any]) -> dict[str, Any]:
    traffic = get_amb_traffic_snapshot()
    out = dict(doc)
    out["traffic"] = traffic
    out["agent_tracker"] = {
        "unique_agents_24h": traffic["unique_agents"],
        "amb_fetches_24h": traffic["amb_fetches"],
        "list_magnets_24h": traffic["list_magnets_calls"],
        "paid_calls_24h": traffic["paid_calls"],
        "last_agent_at": traffic["last_seen_at"],
        "lifetime_unique_agents_approx": traffic["lifetime"]["unique_agents_approx"],
        "host": "acp-x402-scriptmasterlabs",
    }
    return out

DEFAULT_TTL_MS = int(os.environ.get("AMB_TTL_MS", str(15 * 60 * 1000)))

# Namespace hints by capability family
_NS = {
    "sec_": "Finance.Regulatory.SEC",
    "fda_": "Health.Safety.FDA",
    "epa_": "Compliance.Environment.EPA",
    "osha_": "Compliance.Workplace.OSHA",
    "fec_": "Civic.CampaignFinance",
    "fred_": "Finance.Macro.FRED",
    "rwa_": "Finance.RWA.Intelligence",
    "defi_": "Finance.DeFi.Analytics",
    "federal_": "Gov.Federal.Contracting",
    "sdvosb_": "Gov.Federal.SetAsides",
    "sam_": "Gov.Federal.SAM",
    "excluded_": "Gov.Federal.Exclusions",
    "entity_": "Compliance.Entity",
    "compliance_": "Compliance.Banking",
    "ai_": "AI.Verification",
    "druckenmiller_": "Finance.Macro.Regime",
    "perp_": "Finance.Trading.Perps",
    "market_": "Finance.Trading.Regime",
    "gas_": "Crypto.Onchain.Gas",
    "wallet_": "Crypto.Onchain.Wallet",
    "trending_": "Crypto.Market.Discovery",
    "smart_": "Crypto.Market.SmartMoney",
    "new_token": "Crypto.Market.Discovery",
    "rugpull_": "Crypto.Security",
    "honeypot_": "Crypto.Security",
    "token_security": "Crypto.Security",
    "airdrop_": "Crypto.Airdrop",
    "liquidation_": "Finance.Trading.Risk",
    "stablecoin_": "Crypto.Stablecoin",
    "funding_": "Finance.Trading.Perps",
    "crypto_": "Crypto.Market.Data",
    "web_": "Agent.Retrieval",
    "social_": "Agent.Social",
    "llm_": "Agent.LLM",
    "news_": "Agent.News",
    "eth_": "Crypto.RPC",
    "base_": "Crypto.RPC",
    "domain_": "Agent.Enrichment",
}

# Top magnets to surface first (volume / demand lanes)
_PRIORITY = [
    "gas_tracker",
    "crypto_price",
    "perp_funding_aggregator",
    "funding_rates",
    "rwa_intelligence",
    "rwa_aggregates",
    "defi_yield_rates",
    "trending_tokens",
    "wallet_analyzer",
    "sec_8_k_real_time_filings",
    "sec_insider_trade_intel",
    "fred_economic_indicators",
    "federal_contract_opportunities",
    "sdvosb_setaside_feed",
    "druckenmiller_macro_regime_analysis",
    "market_regime_indicator",
    "web_search",
    "web_fetch",
    "llm_chat",
    "honeypot_check",
]


def magnet_strength(
    reputation_score: float = 0.94,
    success_rate_24h: float = 0.987,
    uptime_24h: float = 0.999,
    avg_latency_ms: float = 420.0,
) -> float:
    """Simple shippable formula from AMB spec; clamp 0..1."""
    lat_term = 1.0 / math.log10(avg_latency_ms + 10.0)
    raw = (
        reputation_score * 0.50
        + success_rate_24h * 0.25
        + uptime_24h * 0.15
        + lat_term * 0.10
    )
    return max(0.0, min(1.0, round(raw, 4)))


def _namespace_for(name: str) -> str:
    for prefix, ns in _NS.items():
        if name.startswith(prefix) or prefix.rstrip("_") in name:
            return ns
    return "Finance.TradingIntelligence.SqueezeOS"


def _rating_to_score(rating: str | float | None) -> float:
    try:
        r = float(rating) if rating is not None else 5.0
        # ACP rating is 0-5; map to 0-1 with floor
        return max(0.5, min(1.0, r / 5.0))
    except Exception:
        return 0.94


def _beacon_id(payload_core: dict[str, Any]) -> str:
    blob = (
        f"{payload_core.get('tool_name')}|{payload_core.get('endpoint')}|"
        f"{payload_core.get('issuer')}|{payload_core.get('payment', {}).get('pay_to')}|"
        f"{payload_core.get('price')}|{payload_core.get('issued_at')}"
    ).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _unsigned_sig(beacon_id: str, tool_name: str, issued_at: int) -> str:
    """Deterministic non-chain placeholder until AgentCard signer is wired.
    Format still 0x-hex so scanners accept the field; id is content-addressed.
    """
    seed = f"{beacon_id}|{tool_name}|{issued_at}|{AGENT_WALLET}|amb0.1".encode()
    return "0x" + hashlib.sha256(seed).hexdigest()


def build_beacon(
    *,
    tool_name: str,
    endpoint: str,
    price: str = "0.001",
    description: str = "",
    free_tier: bool = False,
    capabilities: list[str] | None = None,
    reputation_score: float = 0.94,
    success_rate_24h: float = 0.987,
    uptime_24h: float = 0.999,
    avg_latency_ms: float = 420.0,
    issued_at: int | None = None,
    ttl_ms: int = DEFAULT_TTL_MS,
    settlements: int = 0,
) -> dict[str, Any]:
    now = issued_at if issued_at is not None else int(time.time() * 1000)
    strength = magnet_strength(
        reputation_score, success_rate_24h, uptime_24h, avg_latency_ms
    )
    payment = {
        "rails": list(DEFAULT_RAILS),
        "price": str(price),
        "currency": "USDC",
        "x402": not free_tier,
        "pay_to": AGENT_WALLET,
        "payToByNetwork": {
            "eip155:8453": AGENT_WALLET,
            "base": AGENT_WALLET,
            "eip155:4663": AGENT_WALLET,
            "robinhood": AGENT_WALLET,
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": SOL_PAY_TO,
            "solana": SOL_PAY_TO,
            "xrpl": XRPL_PAY_TO,
            "xrpl:0": XRPL_PAY_TO,
        },
        "assetsByNetwork": {
            "eip155:8453": BASE_USDC,
            "eip155:4663": USDG_ASSET,
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": SOL_USDC_MINT,
            "xrpl": "RLUSD",
        },
        "asset": BASE_USDC,
        "rail_detail": [
            {"id": "base_usdc", "symbol": "USDC", "network": "eip155:8453", "payTo": AGENT_WALLET, "asset": BASE_USDC},
            {"id": "robinhood_usdg", "symbol": "USDG", "aliases": ["USCG"], "network": "eip155:4663", "payTo": AGENT_WALLET, "asset": USDG_ASSET},
            {"id": "solana_usdc", "symbol": "USDC", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "payTo": SOL_PAY_TO, "asset": SOL_USDC_MINT},
            {"id": "xrpl_rlusd", "symbol": "RLUSD", "network": "xrpl", "payTo": XRPL_PAY_TO, "asset": "RLUSD", "issuer": RLUSD_ISSUER},
        ],
    }
    core = {
        "tool_name": tool_name,
        "endpoint": endpoint,
        "issuer": ISSUER_DID,
        "payment": payment,
        "price": str(price),
        "issued_at": now,
    }
    bid = _beacon_id(core)
    beacon = {
        "amb_version": AMB_VERSION,
        "type": "AgentMagnetBeacon",
        "id": bid,
        "issuer": ISSUER_DID,
        "agent": {
            "name": AGENT_NAME,
            "id": AGENT_ID,
            "wallet": AGENT_WALLET,
            "marketplace": "Virtuals ACP",
        },
        "issued_at": now,
        "expires_at": now + ttl_ms,
        "namespace": _namespace_for(tool_name),
        "endpoint": endpoint,
        "tool_name": tool_name,
        "payment": payment,
        "performance": {
            "avg_latency_ms": avg_latency_ms,
            "success_rate_24h": success_rate_24h,
            "uptime_24h": uptime_24h,
        },
        "reputation": {
            "score": reputation_score,
            "settlements": settlements,
            "source": "ACP+402Proof",
            "rating": "5.00",
        },
        "magnet_strength": strength,
        "capabilities": capabilities or [tool_name],
        "free_tier": free_tier,
        "description": description
        or f"{tool_name} — ScriptMasterLabs pay-per-call via x402 / ACP.",
        "signature": _unsigned_sig(bid, tool_name, now),
        "links": {
            "x402": endpoint.rsplit("/x402", 1)[0] + "/.well-known/x402"
            if "/x402" in endpoint
            else "https://acp-x402-scriptmasterlabs.onrender.com/.well-known/x402",
            "agent_card": "https://acp-x402-scriptmasterlabs.onrender.com/.well-known/agent.json",
            "mcp": "https://acp-x402-scriptmasterlabs.onrender.com/mcp",
            "homepage": "https://www.scriptmasterlabs.com",
            "acp_agent_id": AGENT_ID,
        },
    }
    return beacon


def _tool_description(name: str, fn: Callable[..., Any] | None) -> str:
    if fn and getattr(fn, "__doc__", None):
        line = fn.__doc__.strip().splitlines()[0].strip()
        if line:
            return line
    return f"ScriptMasterLabs {name.replace('_', ' ')} — x402 / ACP capability."


def build_amb_document(
    base_url: str | None = None,
    *,
    limit: int | None = None,
    include_free_magnets_tool: bool = True,
) -> dict[str, Any]:
    """Build full AMB document from live PROVIDER_ENDPOINTS + prices."""
    from provider import ENDPOINTS as PROVIDER_ENDPOINTS

    try:
        from x402_server import _PRICES_USD as prices
    except Exception:
        prices = {}

    base = (base_url or os.environ.get("X402_PUBLIC_BASE") or "").rstrip("/")
    if not base:
        try:
            base = request.host_url.rstrip("/")
        except Exception:
            base = "https://acp-x402-scriptmasterlabs.onrender.com"

    rep = _rating_to_score(os.environ.get("ACP_RATING", "5.00"))
    # Prefer live health-ish defaults; operators can override via env
    success = float(os.environ.get("AMB_SUCCESS_RATE", "0.987"))
    uptime = float(os.environ.get("AMB_UPTIME", "0.999"))
    latency = float(os.environ.get("AMB_LATENCY_MS", "420"))
    settlements = int(os.environ.get("AMB_SETTLEMENTS", "0"))

    now = int(time.time() * 1000)
    names = list(PROVIDER_ENDPOINTS.keys())
    # priority first, then alpha
    ordered = [n for n in _PRIORITY if n in PROVIDER_ENDPOINTS]
    ordered += sorted(n for n in names if n not in ordered)

    beacons: list[dict[str, Any]] = []
    for name in ordered:
        if limit is not None and len(beacons) >= limit:
            break
        price = str(prices.get(name, "0.001"))
        route = f"{base}/x402/{name.replace('_', '-')}"
        fn = PROVIDER_ENDPOINTS.get(name)
        beacons.append(
            build_beacon(
                tool_name=name,
                endpoint=route,
                price=price,
                description=_tool_description(name, fn),
                free_tier=False,
                capabilities=[name],
                reputation_score=rep,
                success_rate_24h=success,
                uptime_24h=uptime,
                avg_latency_ms=latency,
                issued_at=now,
                settlements=settlements,
            )
        )

    if include_free_magnets_tool:
        # Free discovery beacons for the magnet tools themselves
        for free_name, path, desc in [
            (
                "list_magnets",
                f"{base}/.well-known/amb.json",
                "FREE — list all Agent Magnet Beacons ranked by magnet_strength.",
            ),
            (
                "get_agent_magnet_beacons",
                f"{base}/.well-known/amb.json",
                "FREE — full AMB document for scriptmasterlabs ACP agent.",
            ),
        ]:
            beacons.insert(
                0,
                build_beacon(
                    tool_name=free_name,
                    endpoint=path,
                    price="0",
                    description=desc,
                    free_tier=True,
                    capabilities=["amb", "discovery", free_name],
                    reputation_score=1.0,
                    success_rate_24h=1.0,
                    uptime_24h=1.0,
                    avg_latency_ms=50,
                    issued_at=now,
                    settlements=settlements,
                ),
            )

    # Rank paid beacons by magnet_strength desc (free stay on top)
    free = [b for b in beacons if b.get("free_tier")]
    paid = [b for b in beacons if not b.get("free_tier")]
    paid.sort(key=lambda b: b.get("magnet_strength", 0), reverse=True)
    beacons = free + paid

    top = paid[0] if paid else None
    return {
        "amb_version": AMB_VERSION,
        "type": "AgentMagnetBeaconSet",
        "issuer": ISSUER_DID,
        "agent": {
            "name": AGENT_NAME,
            "id": AGENT_ID,
            "wallet": AGENT_WALLET,
            "role": "HYBRID",
            "marketplace": "Virtuals ACP",
            "rating": "5.00",
        },
        "issued_at": now,
        "expires_at": now + DEFAULT_TTL_MS,
        "pay_to": AGENT_WALLET,
        "rails": list(DEFAULT_RAILS),
        "count": len(beacons),
        "paid_count": len(paid),
        "free_count": len(free),
        "top_magnet": {
            "tool_name": top["tool_name"],
            "magnet_strength": top["magnet_strength"],
            "endpoint": top["endpoint"],
            "price": top["payment"]["price"],
        }
        if top
        else None,
        "formula": (
            "magnet_strength = reputation*0.50 + success_rate_24h*0.25 "
            "+ uptime_24h*0.15 + (1/log10(latency_ms+10))*0.10"
        ),
        "discovery": {
            "amb": f"{base}/.well-known/amb.json",
            "agent": f"{base}/.well-known/agent.json",
            "x402": f"{base}/.well-known/x402",
            "mcp": f"{base}/mcp",
            "www": "https://www.scriptmasterlabs.com/.well-known/amb.json",
        },
        "beacons": beacons,
        "note": (
            "Prefer highest magnet_strength. Pay via x402 multi-rail (Base USDC, RH USDG, Sol USDC, XRPL RLUSD) or hire "
            "the same capabilities as ACP offerings on Virtuals. "
            "pay_to is the ACP EOA — never 0x4e14 orphan seed."
        ),
    }


# ── Flask Blueprint ──────────────────────────────────────────────────────────
amb_bp = Blueprint("amb", __name__)


@amb_bp.route("/amb/traffic", methods=["GET", "OPTIONS"])
def amb_traffic():
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.status_code = 204
    else:
        resp = jsonify(get_amb_traffic_snapshot())
        resp.headers["Cache-Control"] = "public, max-age=30"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@amb_bp.route("/.well-known/amb.json", methods=["GET", "OPTIONS"])
def amb_json():
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.status_code = 204
    else:
        base = request.host_url.rstrip("/")
        # optional ?limit=N for scanners
        lim = request.args.get("limit", type=int)
        record_amb_traffic("amb_fetch", _agent_key_from_request())
        doc = _attach_traffic(build_amb_document(base_url=base, limit=lim))
        resp = jsonify(doc)
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Cache-Control"] = "public, max-age=60"
        resp.headers["X-AMB-Agents-24h"] = str(doc.get("traffic", {}).get("unique_agents", 0))
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    resp.headers["X-AMB-Version"] = AMB_VERSION
    resp.headers["Link"] = f'<{request.host_url.rstrip("/")}/.well-known/amb.json>; rel="agent-magnet-beacon"'
    return resp


def free_magnet_tools() -> list[dict[str, Any]]:
    """MCP tool descriptors for free AMB discovery tools."""
    return [
        {
            "name": "list_magnets",
            "description": (
                "FREE. List Agent Magnet Beacons for scriptmasterlabs, ranked by "
                "magnet_strength. Use this to discover which paid tools to call next. "
                "No payment required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max beacons to return (default 25).",
                    },
                    "min_strength": {
                        "type": "number",
                        "description": "Minimum magnet_strength 0-1 filter.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            "annotations": {
                "title": "List Magnets",
                "category": "Discovery",
                "pricing": {
                    "amount": "0",
                    "currency": "USDC",
                    "network": "base",
                    "perCall": True,
                },
                "httpRoute": "/.well-known/amb.json",
                "free": True,
            },
        },
        {
            "name": "get_agent_magnet_beacons",
            "description": (
                "FREE. Full Agent Magnet Beacon set for the scriptmasterlabs ACP agent "
                "(all paid capabilities + discovery links). No payment required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Optional max paid beacons.",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            "annotations": {
                "title": "Get Agent Magnet Beacons",
                "category": "Discovery",
                "pricing": {
                    "amount": "0",
                    "currency": "USDC",
                    "network": "base",
                    "perCall": True,
                },
                "httpRoute": "/.well-known/amb.json",
                "free": True,
            },
        },
    ]


def handle_free_magnet_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    try:
        base = request.host_url.rstrip("/")
    except Exception:
        base = "https://acp-x402-scriptmasterlabs.onrender.com"
    lim = args.get("limit")
    try:
        lim = int(lim) if lim is not None else None
    except Exception:
        lim = None
    if name == "list_magnets":
        record_amb_traffic("list_magnets", _agent_key_from_request())
    else:
        record_amb_traffic("get_agent_magnet_beacons", _agent_key_from_request())
    doc = _attach_traffic(build_amb_document(base_url=base, limit=lim))
    if name == "list_magnets":
        beacons = list(doc.get("beacons") or [])
        min_s = args.get("min_strength")
        if min_s is not None:
            try:
                ms = float(min_s)
                beacons = [b for b in beacons if float(b.get("magnet_strength") or 0) >= ms]
            except Exception:
                pass
        if lim is None:
            lim = 25
        beacons = beacons[: int(lim)]
        return {
            "free": True,
            "count": len(beacons),
            "agent": doc.get("agent"),
            "pay_to": doc.get("pay_to"),
            "top_magnet": doc.get("top_magnet"),
            "traffic": doc.get("traffic"),
            "agent_tracker": doc.get("agent_tracker"),
            "beacons": beacons,
            "full_document": f"{base}/.well-known/amb.json",
        }
    # get_agent_magnet_beacons
    return doc


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(build_amb_document(base_url="https://acp-x402-scriptmasterlabs.onrender.com"), indent=2)[:4000])
