#!/usr/bin/env python3
"""
scriptmasterlabs RWA Engine — ownable real-world asset intelligence.

No paid RWA data vendors. We own:
  1) Curated asset registry (class, issuer, chains, contracts, notes)
  2) Valuation / risk / aggregate math
  3) Normalized JSON schema for agents

Public feeds used as raw inputs only:
  - DefiLlama /protocols (TVL, category, chains, address)
  - CoinGecko simple/price + markets when not rate-limited
  - Optional public RPC later (holders) — not required for MVP

Disclaimer: informational metrics only. Not NAV attestations, not financial advice.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

UA = "scriptmasterlabs-rwa-engine/1.2 (+https://www.scriptmasterlabs.com)"
CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300.0  # 5 min

# Ownable curated seed — extend over time. IDs are stable SKUs we control.
# llama_name matches DefiLlama protocol name when possible.
RWA_REGISTRY: list[dict[str, Any]] = [
    {
        "id": "buidl",
        "name": "BlackRock BUIDL",
        "symbol": "BUIDL",
        "asset_class": "tokenized_treasuries",
        "issuer": "BlackRock / Securitize",
        "chains": ["ethereum", "multi-chain"],
        "llama_name": "BlackRock BUIDL",
        "llama_slug": "blackrock-buidl",
        "coingecko_id": "blackrock-usd-institutional-digital-liquidity-fund",
        "contracts": {
            # Securitize / BUIDL primary ethereum token (public registry)
            "ethereum": "0x7712c34205737192402172409a8f7ccef8aa2aec"
        },
        "tags": ["treasuries", "institutional", "fund"],
        "notes": "Tokenized USD institutional liquidity fund; TVL from DefiLlama + CG mcap when available.",
    },
    {
        "id": "ousg",
        "name": "Ondo Short-Term US Government Treasuries",
        "symbol": "OUSG",
        "asset_class": "tokenized_treasuries",
        "issuer": "Ondo",
        "chains": ["ethereum", "multi-chain"],
        "llama_name": "Ondo Yield Assets",
        "llama_slug": "ondo-yield-assets",
        "coingecko_id": "ondo-us-dollar-yield",
        "contracts": {},
        "tags": ["treasuries", "ondo", "fund"],
        "notes": "Ondo short-duration US government / yield product sleeve; CG + protocol TVL signals.",
    },

    {
        "id": "usyc",
        "name": "Circle USYC",
        "symbol": "USYC",
        "asset_class": "tokenized_treasuries",
        "issuer": "Circle / Hashnote",
        "chains": ["multi-chain"],
        "llama_name": "Circle USYC",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["treasuries", "circle"],
        "notes": "Short-duration treasury / cash-equivalent style RWA product.",
    },
    {
        "id": "xaut",
        "name": "Tether Gold",
        "symbol": "XAUt",
        "asset_class": "tokenized_commodities",
        "issuer": "Tether",
        "chains": ["ethereum", "multi-chain"],
        "llama_name": "Tether Gold",
        "coingecko_id": "tether-gold",
        "contracts": {"ethereum": "0x68749665ff8d2d112fa859aa293f07a622782f38"},
        "tags": ["gold", "commodity"],
        "notes": "Gold-backed token; market price + TVL used as dual valuation signals.",
    },
    {
        "id": "paxg",
        "name": "Paxos Gold",
        "symbol": "PAXG",
        "asset_class": "tokenized_commodities",
        "issuer": "Paxos",
        "chains": ["ethereum"],
        "llama_name": "Paxos Gold",
        "coingecko_id": "pax-gold",
        "contracts": {"ethereum": "0x45804880de22913dafe09f4980848ece6ecbaf78"},
        "tags": ["gold", "commodity"],
        "notes": "Allocated gold token; strong market-price signal.",
    },
    {
        "id": "ondo",
        "name": "Ondo Finance",
        "symbol": "ONDO",
        "asset_class": "rwa_protocol",
        "issuer": "Ondo",
        "chains": ["ethereum", "multi-chain"],
        "llama_name": "Ondo Yield Assets",
        "coingecko_id": "ondo-finance",
        "contracts": {"ethereum": "0xfaba6f8e4a5e8ab82f62fe7c39859fa577269be3"},
        "tags": ["treasuries", "protocol", "yield"],
        "notes": "RWA protocol / tokenized yield products; protocol TVL + token mcap.",
    },
    {
        "id": "ondo_global_markets",
        "name": "Ondo Global Markets",
        "symbol": "ONDO-GM",
        "asset_class": "tokenized_securities",
        "issuer": "Ondo",
        "chains": ["multi-chain"],
        "llama_name": "Ondo Global Markets",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["stocks", "etf", "securities"],
        "notes": "Tokenized securities / global markets sleeve under Ondo.",
    },
    {
        "id": "maple",
        "name": "Maple Finance",
        "symbol": "SYRUP",
        "asset_class": "private_credit",
        "issuer": "Maple",
        "chains": ["multi-chain"],
        "llama_name": "Maple",
        "coingecko_id": "maple",
        "contracts": {},
        "tags": ["private_credit", "lending"],
        "notes": "On-chain capital markets / credit. TVL as credit book proxy.",
    },
    {
        "id": "centrifuge",
        "name": "Centrifuge Protocol",
        "symbol": "CFG",
        "asset_class": "private_credit",
        "issuer": "Centrifuge",
        "chains": ["multi-chain"],
        "llama_name": "Centrifuge Protocol",
        "coingecko_id": "centrifuge",
        "contracts": {},
        "tags": ["private_credit", "structured_credit"],
        "notes": "Real-world credit pools tokenized via Centrifuge.",
    },
    {
        "id": "spiko",
        "name": "Spiko",
        "symbol": "SPIKO",
        "asset_class": "tokenized_treasuries",
        "issuer": "Spiko",
        "chains": ["multi-chain"],
        "llama_name": "Spiko",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["treasuries", "europe"],
        "notes": "European tokenized T-bill / money-market style products.",
    },
    {
        "id": "superstate_ustb",
        "name": "Superstate USTB",
        "symbol": "USTB",
        "asset_class": "tokenized_treasuries",
        "issuer": "Superstate",
        "chains": ["ethereum"],
        "llama_name": "Superstate",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["treasuries", "fund"],
        "notes": "Short-term US treasury fund token narrative.",
    },

    {
        "id": "franklin_onchain",
        "name": "Franklin Templeton OnChain",
        "symbol": "BENJI",
        "asset_class": "tokenized_treasuries",
        "issuer": "Franklin Templeton",
        "chains": ["multi-chain"],
        "llama_name": "Franklin Templeton",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["treasuries", "institutional", "fund"],
        "notes": "Traditional asset manager on-chain money market / treasury product narrative.",
    },
    {
        "id": "hashnote_usyc",
        "name": "Hashnote USYC",
        "symbol": "USYC",
        "asset_class": "tokenized_treasuries",
        "issuer": "Hashnote / Circle",
        "chains": ["multi-chain"],
        "llama_name": "Hashnote",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["treasuries"],
        "notes": "Short-duration treasury product; may alias with Circle USYC feeds.",
    },
    {
        "id": "mountain_usdm",
        "name": "Mountain Protocol USDM",
        "symbol": "USDM",
        "asset_class": "tokenized_treasuries",
        "issuer": "Mountain Protocol",
        "chains": ["multi-chain"],
        "llama_name": "Mountain Protocol",
        "coingecko_id": "mountain-protocol-usdm",
        "contracts": {},
        "tags": ["treasuries", "yield_stable"],
        "notes": "Yield-bearing stable / T-bill backed style dollar.",
    },
    {
        "id": "goldfinch",
        "name": "Goldfinch",
        "symbol": "GFI",
        "asset_class": "private_credit",
        "issuer": "Goldfinch",
        "chains": ["ethereum"],
        "llama_name": "Goldfinch",
        "coingecko_id": "goldfinch",
        "contracts": {},
        "tags": ["private_credit", "emerging_markets"],
        "notes": "Private credit protocol with real-world borrower pools.",
    },
    {
        "id": "backedu",
        "name": "Backed Finance",
        "symbol": "BACKED",
        "asset_class": "tokenized_securities",
        "issuer": "Backed",
        "chains": ["multi-chain"],
        "llama_name": "Backed",
        "coingecko_id": None,
        "contracts": {},
        "tags": ["stocks", "etf", "securities"],
        "notes": "Tokenized securities / bTokens style products.",
    },
    {
        "id": "polymesh_rwa",
        "name": "Polymesh",
        "symbol": "POLYX",
        "asset_class": "rwa_protocol",
        "issuer": "Polymesh",
        "chains": ["polymesh"],
        "llama_name": "Polymesh",
        "coingecko_id": "polymesh",
        "contracts": {},
        "tags": ["securities", "compliance_chain"],
        "notes": "Securities-focused L1 / infrastructure for regulated assets.",
    },

]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_json(url: str, ttl: float = CACHE_TTL) -> Any:
    hit = CACHE.get(url)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}", "url": url, "detail": str(e.reason)}
    except Exception as e:
        return {"error": "fetch_failed", "url": url, "detail": str(e)[:200]}
    CACHE[url] = (time.time(), data)
    return data


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def fetch_llama_rwa_protocols() -> list[dict[str, Any]]:
    data = _http_json("https://api.llama.fi/protocols", ttl=600)
    if not isinstance(data, list):
        return []
    out = []
    for p in data:
        cat = (p.get("category") or "").lower()
        name = p.get("name") or ""
        desc = (p.get("description") or "").lower()
        if cat == "rwa" or "rwa" in desc or "real world" in desc or "tokenized" in desc:
            out.append(p)
        elif _norm(name) in {_norm(r["llama_name"]) for r in RWA_REGISTRY if r.get("llama_name")}:
            out.append(p)
    return out


def fetch_coingecko_prices(ids: list[str]) -> dict[str, Any]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    # batch
    q = ",".join(sorted(set(ids)))
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={q}&vs_currencies=usd&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
    )
    data = _http_json(url, ttl=120)
    return data if isinstance(data, dict) and "error" not in data else {}


def _match_llama(reg: dict[str, Any], protocols: list[dict[str, Any]]) -> dict[str, Any] | None:
    target = _norm(reg.get("llama_name") or reg.get("name"))
    if not target:
        return None
    for p in protocols:
        if _norm(p.get("name")) == target:
            return p
    # fuzzy contains
    for p in protocols:
        n = _norm(p.get("name"))
        if target in n or n in target:
            return p
    return None


def _risk_score(asset: dict[str, Any], llama: dict[str, Any] | None, px: dict[str, Any] | None) -> dict[str, Any]:
    """Heuristic ownable risk model 0-100 (higher = more risk). Not a credit rating."""
    score = 40
    factors: list[str] = []

    tvl = float((llama or {}).get("tvl") or 0)
    if tvl >= 1_000_000_000:
        score -= 12
        factors.append("tvl_over_1b_liquidity_depth")
    elif tvl >= 100_000_000:
        score -= 6
        factors.append("tvl_over_100m")
    elif tvl < 10_000_000:
        score += 15
        factors.append("tvl_under_10m_thin_book")

    mcap = float((px or {}).get("usd_market_cap") or 0)
    if mcap and tvl:
        ratio = mcap / max(tvl, 1)
        if ratio > 5:
            score += 10
            factors.append("token_mcap_far_above_protocol_tvl")
        elif ratio < 0.2:
            factors.append("token_mcap_small_vs_tvl_governance_lite")

    change = (px or {}).get("usd_24h_change")
    if isinstance(change, (int, float)) and abs(change) > 8:
        score += 8
        factors.append("high_24h_volatility")

    cls = asset.get("asset_class") or ""
    if cls in ("tokenized_treasuries", "tokenized_commodities"):
        score -= 8
        factors.append("simpler_underlying_class")
    if cls == "private_credit":
        score += 6
        factors.append("private_credit_opacity")
    if cls == "rwa_protocol":
        score += 4
        factors.append("protocol_token_not_claim_on_nav")

    if not asset.get("contracts"):
        score += 5
        factors.append("missing_primary_contract_map")
    if not llama:
        score += 10
        factors.append("no_live_tvl_feed_match")

    # institutional names get a mild trust bump (still not PoR)
    issuer = (asset.get("issuer") or "").lower()
    if any(k in issuer for k in ["blackrock", "circle", "paxos", "franklin", "fidelity"]):
        score -= 10
        factors.append("known_institutional_issuer")

    score = max(5, min(95, int(round(score))))
    band = "low" if score < 35 else "moderate" if score < 55 else "elevated" if score < 75 else "high"
    return {
        "risk_score": score,
        "risk_band": band,
        "factors": factors,
        "model": "sml_rwa_heuristic_v1",
        "not_a_credit_rating": True,
    }


def _valuation(asset: dict[str, Any], llama: dict[str, Any] | None, px: dict[str, Any] | None) -> dict[str, Any]:
    tvl = float((llama or {}).get("tvl") or 0) or None
    price = (px or {}).get("usd")
    mcap = (px or {}).get("usd_market_cap")
    vol = (px or {}).get("usd_24h_vol")
    chg = (px or {}).get("usd_24h_change")

    # Fair-value style composite for agents:
    # - For fund-like RWAs: TVL is primary AUM proxy
    # - For commodity tokens: market price * implied exposure; TVL secondary
    # - For protocol tokens: separate token mcap from RWA AUM
    primary = None
    method = []
    if asset.get("asset_class") in ("tokenized_treasuries", "private_credit", "tokenized_securities"):
        if tvl:
            primary = tvl
            method.append("protocol_tvl_as_aum_proxy")
        if mcap:
            method.append("governance_or_receipt_token_mcap_secondary")
    elif asset.get("asset_class") == "tokenized_commodities":
        if mcap:
            primary = float(mcap)
            method.append("spot_market_cap")
        if tvl:
            method.append("protocol_tvl_cross_check")
            if primary is None:
                primary = tvl
    else:
        if tvl and mcap:
            primary = float(tvl)
            method.append("tvl_primary_token_mcap_secondary")
        elif tvl:
            primary = tvl
            method.append("tvl_only")
        elif mcap:
            primary = float(mcap)
            method.append("mcap_only")

    confidence = 0.35
    if tvl and (price or mcap):
        confidence = 0.7
    elif tvl:
        confidence = 0.55
    elif mcap:
        confidence = 0.45
    if asset.get("asset_class") == "tokenized_treasuries" and tvl:
        confidence = min(0.85, confidence + 0.1)

    return {
        "as_of": _now(),
        "primary_value_usd": round(primary, 2) if isinstance(primary, (int, float)) else None,
        "primary_value_basis": method[0] if method else None,
        "methods": method,
        "confidence_0_to_1": round(confidence, 2),
        "signals": {
            "protocol_tvl_usd": round(tvl, 2) if tvl else None,
            "token_price_usd": price,
            "token_market_cap_usd": mcap,
            "token_volume_24h_usd": vol,
            "token_change_24h_pct": chg,
            "llama_category": (llama or {}).get("category"),
            "llama_chains": (llama or {}).get("chains") or ([llama.get("chain")] if llama and llama.get("chain") else []),
            "llama_url": (llama or {}).get("url"),
            "llama_address": (llama or {}).get("address"),
        },
        "disclaimer": "Informational composite from live public feeds (DefiLlama TVL, CoinGecko markets). Not a custodian-audited NAV letter. Not investment advice.",
    }


def build_asset_snapshot(reg: dict[str, Any], protocols: list[dict[str, Any]], prices: dict[str, Any]) -> dict[str, Any]:
    llama = _match_llama(reg, protocols)
    px = prices.get(reg["coingecko_id"]) if reg.get("coingecko_id") else None
    if not isinstance(px, dict):
        px = None
    val = _valuation(reg, llama, px)
    risk = _risk_score(reg, llama, px)
    integrity = build_source_integrity(reg, llama, px)
    return {
        "id": reg["id"],
        "name": reg["name"],
        "symbol": reg.get("symbol"),
        "asset_class": reg.get("asset_class"),
        "issuer": reg.get("issuer"),
        "chains": reg.get("chains") or [],
        "contracts": reg.get("contracts") or {},
        "tags": reg.get("tags") or [],
        "notes": reg.get("notes"),
        "valuation": val,
        "risk": risk,
        "source_integrity": {
            "algorithm": integrity["algorithm"],
            "hash": integrity["hash"],
            "method": integrity["method"],
        },
        "sources": {
            "registry": "scriptmasterlabs_curated_v2",
            "tvl_feed": "defillama_protocols" if llama else None,
            "market_feed": "coingecko_simple_price" if px else None,
            "live": True,
            "synthetic": False,
        },
    }



def fetch_protocol_detail(slug: str) -> dict[str, Any]:
    if not slug:
        return {}
    data = _http_json(f"https://api.llama.fi/protocol/{slug}", ttl=600)
    return data if isinstance(data, dict) and "error" not in data else {}


def fetch_tvl_history(slug: str, days: int = 30) -> list[dict[str, Any]]:
    """Live DefiLlama protocol TVL timeseries (not synthetic)."""
    detail = fetch_protocol_detail(slug)
    series = detail.get("tvl") if isinstance(detail, dict) else None
    if not isinstance(series, list):
        return []
    cutoff = time.time() - max(1, days) * 86400
    out = []
    for pt in series:
        if not isinstance(pt, dict):
            continue
        ts = pt.get("date")
        val = pt.get("totalLiquidityUSD")
        if ts is None or val is None:
            continue
        try:
            ts_f = float(ts)
            if ts_f > 1e12:  # ms
                ts_f /= 1000.0
            if ts_f < cutoff:
                continue
            out.append({
                "timestamp": datetime.fromtimestamp(ts_f, tz=timezone.utc).isoformat(),
                "tvl_usd": round(float(val), 2),
            })
        except Exception:
            continue
    return out[-max(days, 1):]


def fetch_coingecko_market_chart(cg_id: str, days: int = 30) -> list[dict[str, Any]]:
    if not cg_id:
        return []
    url = (
        f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
        f"?vs_currency=usd&days={max(1, min(days, 90))}"
    )
    data = _http_json(url, ttl=300)
    prices = data.get("prices") if isinstance(data, dict) else None
    if not isinstance(prices, list):
        return []
    out = []
    for row in prices:
        try:
            ms, px = row[0], row[1]
            out.append({
                "timestamp": datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat(),
                "price_usd": float(px),
            })
        except Exception:
            continue
    # downsample to ~daily-ish if dense
    if len(out) > days * 2:
        step = max(1, len(out) // days)
        out = out[::step]
    return out


def _canonical_source_blob(asset: dict[str, Any], llama: dict[str, Any] | None, px: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical live fields used for reserve/source integrity hash."""
    return {
        "id": asset.get("id"),
        "symbol": asset.get("symbol"),
        "asset_class": asset.get("asset_class"),
        "issuer": asset.get("issuer"),
        "contracts": asset.get("contracts") or {},
        "protocol_tvl_usd": (llama or {}).get("tvl"),
        "llama_name": (llama or {}).get("name"),
        "llama_slug": (llama or {}).get("slug"),
        "llama_address": (llama or {}).get("address"),
        "llama_category": (llama or {}).get("category"),
        "llama_chains": (llama or {}).get("chains"),
        "token_price_usd": (px or {}).get("usd"),
        "token_market_cap_usd": (px or {}).get("usd_market_cap"),
        "token_volume_24h_usd": (px or {}).get("usd_24h_vol"),
        "as_of": _now(),
        "feeds": ["defillama_protocols", "coingecko_simple_price"],
    }


def build_source_integrity(asset: dict[str, Any], llama: dict[str, Any] | None, px: dict[str, Any] | None) -> dict[str, Any]:
    import hashlib
    blob = _canonical_source_blob(asset, llama, px)
    raw = json.dumps(blob, sort_keys=True, separators=(",", ":"), default=str).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "algorithm": "sha256",
        "hash": digest,
        "hashed_fields": sorted(blob.keys()),
        "method": "live_public_source_snapshot",
        "verifiable": True,
        "note": (
            "SHA-256 over canonical live public-source snapshot (TVL/mcap/contracts). "
            "Not a custodian attestation letter. Agents can re-fetch feeds and recompute."
        ),
        "snapshot": {
            "protocol_tvl_usd": blob.get("protocol_tvl_usd"),
            "token_market_cap_usd": blob.get("token_market_cap_usd"),
            "token_price_usd": blob.get("token_price_usd"),
            "contracts": blob.get("contracts"),
            "llama_name": blob.get("llama_name"),
            "as_of": blob.get("as_of"),
        },
    }


def get_proof_of_reserves(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Source-integrity proof from live feeds (recomputable). Not synthetic demo hashes."""
    params = params or {}
    asset_id = (params.get("id") or params.get("asset_id") or params.get("symbol") or "").strip()
    protocols = fetch_llama_rwa_protocols()
    prices = fetch_coingecko_prices([r["coingecko_id"] for r in RWA_REGISTRY if r.get("coingecko_id")])

    if asset_id:
        val = get_valuation({"id": asset_id})
        if val.get("error"):
            return val
        asset = val.get("asset") or {}
        # rebuild llama/px for hash consistency
        reg = None
        for r in RWA_REGISTRY:
            if asset.get("id") == r["id"]:
                reg = r
                break
        llama = _match_llama(reg, protocols) if reg else None
        px = prices.get(reg["coingecko_id"]) if reg and reg.get("coingecko_id") else None
        if not isinstance(px, dict):
            px = None
        # prefer signals already on asset valuation
        if not llama and asset.get("valuation"):
            sig = (asset.get("valuation") or {}).get("signals") or {}
            llama = {
                "tvl": sig.get("protocol_tvl_usd"),
                "name": asset.get("name"),
                "address": sig.get("llama_address"),
                "category": sig.get("llama_category"),
                "chains": sig.get("llama_chains"),
                "slug": (reg or {}).get("llama_slug"),
            }
        por = build_source_integrity(asset if not reg else {**reg, **{k: asset.get(k) for k in ("id","name","symbol","asset_class","issuer","contracts")}}, llama, px)
        return {
            "timestamp": _now(),
            "asset_id": asset.get("id"),
            "name": asset.get("name"),
            "symbol": asset.get("symbol"),
            "primary_value_usd": (asset.get("valuation") or {}).get("primary_value_usd"),
            "proof": por,
            "engine": "scriptmasterlabs_rwa_v2",
            "disclaimer": "Live source-integrity hash — not a custodian legal PoR letter.",
        }

    # aggregate over curated registry
    rows = []
    for reg in RWA_REGISTRY:
        llama = _match_llama(reg, protocols)
        px = prices.get(reg["coingecko_id"]) if reg.get("coingecko_id") else None
        if not isinstance(px, dict):
            px = None
        snap = build_asset_snapshot(reg, protocols, prices)
        por = build_source_integrity(reg, llama, px)
        rows.append({
            "asset_id": reg["id"],
            "name": reg["name"],
            "symbol": reg.get("symbol"),
            "primary_value_usd": (snap.get("valuation") or {}).get("primary_value_usd"),
            "proof_hash": por["hash"],
            "protocol_tvl_usd": (llama or {}).get("tvl"),
        })
    import hashlib
    agg_blob = json.dumps({r["asset_id"]: r["proof_hash"] for r in rows}, sort_keys=True, separators=(",", ":")).encode()
    return {
        "timestamp": _now(),
        "total_assets": len(rows),
        "aggregate_hash": hashlib.sha256(agg_blob).hexdigest(),
        "assets": rows,
        "method": "live_public_source_snapshot",
        "engine": "scriptmasterlabs_rwa_v2",
        "disclaimer": "Aggregate of live source-integrity hashes — not custodian legal PoR.",
    }


def get_valuation_with_history(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    try:
        days = int(params.get("days") or 30)
    except Exception:
        days = 30
    days = max(1, min(days, 90))
    base = get_valuation(params)
    if base.get("error"):
        return base
    asset = base.get("asset") or {}
    reg = None
    for r in RWA_REGISTRY:
        if asset.get("id") == r["id"]:
            reg = r
            break
    history = {"tvl": [], "price": []}
    sources_used = []
    if reg and reg.get("llama_slug"):
        history["tvl"] = fetch_tvl_history(reg["llama_slug"], days=days)
        if history["tvl"]:
            sources_used.append("defillama_protocol_tvl_history")
    elif reg and reg.get("llama_name"):
        # try slugify name
        slug = re.sub(r"[^a-z0-9]+", "-", (reg.get("llama_name") or "").lower()).strip("-")
        history["tvl"] = fetch_tvl_history(slug, days=days)
        if history["tvl"]:
            sources_used.append("defillama_protocol_tvl_history")
    if reg and reg.get("coingecko_id"):
        history["price"] = fetch_coingecko_market_chart(reg["coingecko_id"], days=days)
        if history["price"]:
            sources_used.append("coingecko_market_chart")
    # range stats from whichever series exists
    series_vals = [p["tvl_usd"] for p in history["tvl"]] or [p["price_usd"] for p in history["price"]]
    hist_stats = None
    if series_vals:
        hist_stats = {
            "points": len(series_vals),
            "min": round(min(series_vals), 2),
            "max": round(max(series_vals), 2),
            "last": round(series_vals[-1], 2),
            "change_pct": round((series_vals[-1] / series_vals[0] - 1) * 100, 4) if series_vals[0] else None,
            "window_days": days,
        }
    base["history"] = history
    base["history_stats"] = hist_stats
    base["history_sources"] = sources_used
    base["engine"] = "scriptmasterlabs_rwa_v2"
    return base


def list_assets(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    asset_class = (params.get("asset_class") or params.get("class") or "").strip().lower()
    chain = (params.get("chain") or "").strip().lower()
    q = (params.get("q") or params.get("query") or "").strip().lower()
    min_tvl = params.get("min_tvl_usd") or params.get("min_tvl")
    try:
        min_tvl_f = float(min_tvl) if min_tvl not in (None, "") else None
    except Exception:
        min_tvl_f = None
    try:
        limit = min(int(params.get("limit") or 25), 100)
    except Exception:
        limit = 25
    max_risk = params.get("max_risk") or params.get("risk_lt")
    try:
        max_risk_f = float(max_risk) if max_risk not in (None, "") else None
    except Exception:
        max_risk_f = None
    # constraint DSL: "class=tokenized_treasuries,risk<40,min_tvl=1e8"
    constraint = (params.get("constraint") or params.get("where") or "").strip()
    if constraint:
        for part in re.split(r"[;,]", constraint):
            part=part.strip()
            if not part:
                continue
            m=re.match(r"class\s*=\s*([\w\-]+)", part, re.I)
            if m and not asset_class:
                asset_class=m.group(1).lower()
            m=re.match(r"risk\s*<\s*([0-9.]+)", part, re.I)
            if m and max_risk_f is None:
                max_risk_f=float(m.group(1))
            m=re.match(r"min_tvl\s*=\s*([0-9.eE+]+)", part, re.I)
            if m and min_tvl_f is None:
                min_tvl_f=float(m.group(1))
            m=re.match(r"chain\s*=\s*([\w\-]+)", part, re.I)
            if m and not chain:
                chain=m.group(1).lower()

    protocols = fetch_llama_rwa_protocols()
    cg_ids = [r["coingecko_id"] for r in RWA_REGISTRY if r.get("coingecko_id")]
    prices = fetch_coingecko_prices(cg_ids)

    rows = [build_asset_snapshot(r, protocols, prices) for r in RWA_REGISTRY]

    # Also surface top unmatched Llama RWA protocols as discovery rows (ownable enrichment layer later)
    matched_names = {_norm(r.get("llama_name") or r.get("name")) for r in RWA_REGISTRY}
    extras = []
    for p in sorted(protocols, key=lambda x: -(x.get("tvl") or 0)):
        if _norm(p.get("name")) in matched_names:
            continue
        if (p.get("category") or "").lower() != "rwa":
            continue
        extras.append(
            {
                "id": f"llama:{_norm(p.get('name'))[:40]}",
                "name": p.get("name"),
                "symbol": p.get("symbol"),
                "asset_class": "unclassified_rwa",
                "issuer": None,
                "chains": p.get("chains") or ([p.get("chain")] if p.get("chain") else []),
                "contracts": {"primary": p.get("address")} if p.get("address") else {},
                "tags": ["defillama_rwa", "discovery"],
                "notes": "Auto-discovered RWA-category protocol — not yet fully curated in SML registry.",
                "valuation": {
                    "as_of": _now(),
                    "primary_value_usd": p.get("tvl"),
                    "primary_value_basis": "protocol_tvl_as_aum_proxy",
                    "methods": ["protocol_tvl_as_aum_proxy"],
                    "confidence_0_to_1": 0.4,
                    "signals": {
                        "protocol_tvl_usd": p.get("tvl"),
                        "llama_category": p.get("category"),
                        "llama_url": p.get("url"),
                    },
                    "disclaimer": "Discovery row only. Curate into registry for full risk model.",
                },
                "risk": {
                    "risk_score": 60,
                    "risk_band": "elevated",
                    "factors": ["uncurated_discovery"],
                    "model": "sml_rwa_heuristic_v1",
                    "not_a_credit_rating": True,
                },
                "sources": {"registry": None, "tvl_feed": "defillama_protocols", "market_feed": None},
            }
        )
        if len(extras) >= 30:
            break

    rows.extend(extras)

    def ok(row: dict[str, Any]) -> bool:
        if asset_class and asset_class not in (row.get("asset_class") or "").lower():
            return False
        if chain:
            chains = " ".join([str(c).lower() for c in (row.get("chains") or [])])
            if chain not in chains and chain not in json.dumps(row.get("contracts") or {}).lower():
                return False
        if q:
            blob = json.dumps(row).lower()
            if q not in blob:
                return False
        if min_tvl_f is not None:
            pv = (row.get("valuation") or {}).get("primary_value_usd") or 0
            try:
                if float(pv or 0) < min_tvl_f:
                    return False
            except Exception:
                return False
        if max_risk_f is not None:
            rs = ((row.get("risk") or {}).get("risk_score"))
            try:
                if rs is None or float(rs) >= max_risk_f:
                    return False
            except Exception:
                return False
        return True

    filtered = [r for r in rows if ok(r)]
    filtered.sort(key=lambda r: float((r.get("valuation") or {}).get("primary_value_usd") or 0), reverse=True)
    filtered = filtered[:limit]

    total_tvl = sum(float((r.get("valuation") or {}).get("primary_value_usd") or 0) for r in filtered)
    by_class: dict[str, float] = {}
    for r in filtered:
        cls = r.get("asset_class") or "unknown"
        by_class[cls] = by_class.get(cls, 0.0) + float((r.get("valuation") or {}).get("primary_value_usd") or 0)

    return {
        "timestamp": _now(),
        "count": len(filtered),
        "total_primary_value_usd": round(total_tvl, 2),
        "by_asset_class_usd": {k: round(v, 2) for k, v in sorted(by_class.items(), key=lambda kv: -kv[1])},
        "assets": filtered,
        "filters": {
            "asset_class": asset_class or None,
            "chain": chain or None,
            "q": q or None,
            "min_tvl_usd": min_tvl_f,
            "max_risk": max_risk_f,
            "constraint": constraint or None,
            "limit": limit,
        },
        "registry_size": len(RWA_REGISTRY),
        "engine": "scriptmasterlabs_rwa_v2",
        "disclaimer": "Ownable SML metrics over live public feeds. Source-integrity hashes included. Not a custodian legal PoR letter.",
    }


def get_valuation(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    asset_id = (params.get("id") or params.get("asset_id") or params.get("symbol") or "").strip().lower()
    if not asset_id:
        return {"error": "missing_id", "hint": "Pass id (e.g. buidl, ondo, paxg) or symbol"}

    protocols = fetch_llama_rwa_protocols()
    prices = fetch_coingecko_prices([r["coingecko_id"] for r in RWA_REGISTRY if r.get("coingecko_id")])

    reg = None
    for r in RWA_REGISTRY:
        if asset_id in {_norm(r["id"]), _norm(r.get("symbol")), _norm(r.get("name"))}:
            reg = r
            break
    if not reg:
        # discovery fallback via llama name
        for p in protocols:
            if asset_id in _norm(p.get("name")) or asset_id in _norm(p.get("symbol")):
                snap = {
                    "id": f"llama:{_norm(p.get('name'))[:40]}",
                    "name": p.get("name"),
                    "symbol": p.get("symbol"),
                    "asset_class": "unclassified_rwa",
                    "issuer": None,
                    "chains": p.get("chains") or [],
                    "contracts": {"primary": p.get("address")} if p.get("address") else {},
                    "tags": ["discovery"],
                    "notes": "Uncurated discovery valuation",
                }
                return {
                    "timestamp": _now(),
                    "asset": snap,
                    "valuation": _valuation(snap, p, None),
                    "risk": _risk_score(snap, p, None),
                    "engine": "scriptmasterlabs_rwa_v2",
                }
        return {"error": "not_found", "id": asset_id, "known_ids": [r["id"] for r in RWA_REGISTRY]}

    snap = build_asset_snapshot(reg, protocols, prices)
    return {"timestamp": _now(), "asset": snap, "engine": "scriptmasterlabs_rwa_v2"}


def get_risk(params: dict[str, Any] | None = None) -> dict[str, Any]:
    val = get_valuation(params)
    if val.get("error"):
        return val
    asset = val.get("asset") or {}
    return {
        "timestamp": _now(),
        "id": asset.get("id"),
        "name": asset.get("name"),
        "asset_class": asset.get("asset_class"),
        "risk": asset.get("risk"),
        "valuation_primary_usd": (asset.get("valuation") or {}).get("primary_value_usd"),
        "engine": "scriptmasterlabs_rwa_v2",
    }


def aggregates(params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = list_assets({"limit": 100, **(params or {})})
    assets = data.get("assets") or []
    return {
        "timestamp": _now(),
        "asset_count": len(assets),
        "total_primary_value_usd": data.get("total_primary_value_usd"),
        "by_asset_class_usd": data.get("by_asset_class_usd"),
        "top_assets": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "asset_class": a.get("asset_class"),
                "primary_value_usd": (a.get("valuation") or {}).get("primary_value_usd"),
                "risk_score": (a.get("risk") or {}).get("risk_score"),
            }
            for a in assets[:10]
        ],
        "engine": "scriptmasterlabs_rwa_v2",
        "disclaimer": data.get("disclaimer"),
    }


def rwa_intelligence(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Unified agent entrypoint. action=list|valuation|risk|aggregates|por (default list)."""
    params = dict(params or {})
    action = (params.get("action") or params.get("op") or "list").strip().lower()
    if action in ("list", "assets", "search"):
        payload = list_assets(params)
    elif action in ("valuation", "value", "nav", "quote"):
        # include live history by default for premium intelligence
        payload = get_valuation_with_history(params) if "get_valuation_with_history" in globals() else get_valuation(params)
    elif action in ("risk", "score"):
        payload = get_risk(params)
    elif action in ("aggregates", "aggregate", "summary", "tvl"):
        payload = aggregates(params)
    elif action in ("por", "proof", "proof_of_reserves", "reserves"):
        payload = get_proof_of_reserves(params)
    else:
        payload = {
            "error": "unknown_action",
            "allowed": ["list", "valuation", "risk", "aggregates", "por"],
            "example": {"action": "valuation", "id": "buidl"},
        }
    # ACP deliverable convention
    return {"result": json.dumps(payload, default=str)}


if __name__ == "__main__":
    print(json.dumps(json.loads(rwa_intelligence({"action": "aggregates"})["result"]), indent=2)[:2000])
