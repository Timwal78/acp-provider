#!/usr/bin/env python3
"""
beacon.py — x402-BEACON Engine (Script Master Labs)

Pillars (live, no new Render service):
  1. Universal Manifest   GET /.well-known/x402.json  (+ /beacon/manifest)
  2. Intent Indexer       POST /beacon/query  (keyword+tag vectors, in-process)
  3. Bid Negotiation      headers on 402 + POST /beacon/negotiate
  4. Attestation Trust    POST /beacon/attest · GET /beacon/reputation/{tool}

Money path stays existing x402 settle → payTo 0x7233… only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import fcntl
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

# ── Identity (never orphan 0x4e14) ──────────────────────────────────────────
AGENT_NAME = "scriptmasterlabs"
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
AGENT_WALLET = (
    os.environ.get("X402_PAY_TO")
    or os.environ.get("ACP_AGENT_WALLET_ADDRESS")
    or "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
).strip()
if AGENT_WALLET.lower().startswith("0x4e14"):
    AGENT_WALLET = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"

SOL_PAY_TO = (
    os.environ.get("SOLANA_PAY_TO")
    or "E4d3JwcTjeqTRkkQS4moszcfa4R7G1NMgPSew4KBNFrB"
).strip()
XRPL_PAY_TO = (
    os.environ.get("XRPL_PAY_TO")
    or "rNduuviQ3CCvHqWUTjJDD82Ko2tjqFGs3q"
).strip()

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDG_ASSET = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RLUSD_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"

BEACON_VERSION = "1.0.0"
FLOOR_USD = 0.001
DEFAULT_BASE = "https://acp-x402-scriptmasterlabs.onrender.com"

_STORE = Path(os.environ.get("BEACON_STORE", "/tmp/sml_beacon_store.json"))

# ── Tag lexicon for intent vectors (no heavy ML dep) ────────────────────────
_TAG_VOCAB = [
    "gas", "price", "crypto", "defi", "tvl", "yield", "funding", "perp",
    "rwa", "treasury", "sec", "filing", "fda", "compliance", "federal",
    "sdvosb", "rpc", "base", "ethereum", "solana", "wallet", "honeypot",
    "rugpull", "news", "search", "web", "macro", "options", "volatility",
    "stablecoin", "fast", "cheap", "latency", "market", "token", "trending",
    "intelligence", "risk", "audit", "llm", "chat", "weather", "fx",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _empty_store() -> dict[str, Any]:
    return {"attestations": [], "reputation": {}, "crawl": []}


def _load_store() -> dict[str, Any]:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        if not _STORE.exists():
            return _empty_store()
        with open(_STORE, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                raw = fh.read()
                data = json.loads(raw) if raw else _empty_store()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        if not isinstance(data, dict):
            return _empty_store()
        data.setdefault("attestations", [])
        data.setdefault("reputation", {})
        data.setdefault("crawl", [])
        return data
    except Exception:
        return _empty_store()


def _save_store(data: dict[str, Any]) -> None:
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        tmp.replace(_STORE)
    except Exception:
        pass


def _public_key_hex() -> str:
    """Deterministic ed25519-style placeholder bound to payTo (content id)."""
    seed = f"beacon-pk|{AGENT_WALLET}|{BEACON_VERSION}".encode()
    return "ed25519_pk_" + hashlib.sha256(seed).hexdigest()[:40]


def _tokenize(text: str) -> dict[str, float]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    # expand snake_case
    expanded = []
    for t in toks:
        expanded.append(t)
        for p in t.split("_"):
            if p:
                expanded.append(p)
    counts: dict[str, float] = {}
    for t in expanded:
        if t in _TAG_VOCAB or len(t) > 3:
            counts[t] = counts.get(t, 0.0) + 1.0
    # always project onto vocab dims + free dims
    vec: dict[str, float] = {k: 0.0 for k in _TAG_VOCAB}
    for k, v in counts.items():
        if k in vec:
            vec[k] += v
        else:
            vec[k] = v
    # l2 norm
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items() if v > 0}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values())) or 1.0
    nb = math.sqrt(sum(v * v for v in b.values())) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _hyphen(name: str) -> str:
    return name.replace("_", "-")


def _load_prices() -> dict[str, str]:
    try:
        from x402_server import _PRICES_USD  # type: ignore

        return {k: str(v) for k, v in _PRICES_USD.items()}
    except Exception:
        try:
            from catalog_extra import EXTRA_PRICES_USD  # type: ignore

            return {k: str(v) for k, v in EXTRA_PRICES_USD.items()}
        except Exception:
            return {
                "gas_tracker": "0.001",
                "crypto_price": "0.001",
                "rwa_aggregates": "0.001",
                "base_rpc": "0.001",
            }


def _load_endpoints() -> list[str]:
    try:
        from provider import ENDPOINTS  # type: ignore

        return sorted(ENDPOINTS.keys())
    except Exception:
        return sorted(_load_prices().keys())


def reputation_for(tool: str) -> dict[str, Any]:
    store = _load_store()
    rep = (store.get("reputation") or {}).get(tool) or {}
    score = float(rep.get("score", 0.92))
    n = int(rep.get("n", 0))
    avg_lat = float(rep.get("avg_latency_ms", 420.0))
    ok_rate = float(rep.get("ok_rate", 0.99))
    return {
        "tool": tool,
        "score": round(score, 4),
        "attestations": n,
        "avg_latency_ms": round(avg_lat, 1),
        "ok_rate": round(ok_rate, 4),
        "source": "beacon_peer_attest_v1",
    }


def build_capability(tool: str, base: str, price: str) -> dict[str, Any]:
    route = f"{base}/x402/{_hyphen(tool)}"
    tags = list(_tokenize(tool).keys())[:12]
    rep = reputation_for(tool)
    # dynamic range: floor = max(FLOOR, price*0.8), ceil = price*3 for bid room
    try:
        p = float(price)
    except Exception:
        p = FLOOR_USD
    floor = max(FLOOR_USD, round(p * 0.8, 6))
    ceil = round(max(p * 3.0, FLOOR_USD * 5), 6)
    return {
        "id": tool,
        "name": tool,
        "route": route,
        "method": "GET",
        "capabilities": tags or [tool],
        "pricing": {
            "base_rate": str(price),
            "asset": "USDC",
            "network": "eip155:8453",
            "floor": str(floor),
            "ceiling": str(ceil),
            "dynamic_bidding": True,
            "batch_discount": True,
        },
        "sla_min_latency_ms": 50 if "rpc" in tool or "gas" in tool else 200,
        "reputation": rep,
        "schema": {
            "input": {"type": "object", "additionalProperties": True},
            "output": {"type": "object", "required": ["result"]},
        },
        "vector": _tokenize(f"{tool} {' '.join(tags)}"),
    }


def build_manifest(base_url: str | None = None, limit: int | None = None) -> dict[str, Any]:
    base = (base_url or DEFAULT_BASE).rstrip("/")
    prices = _load_prices()
    tools = _load_endpoints()
    # lead money wedges
    lead = ["gas_tracker", "crypto_price", "rwa_aggregates", "base_rpc", "web_search"]
    ordered = [t for t in lead if t in tools] + [t for t in tools if t not in lead]
    if limit:
        ordered = ordered[: int(limit)]
    caps = [build_capability(t, base, prices.get(t, "0.001")) for t in ordered]
    return {
        "beacon_version": BEACON_VERSION,
        "type": "X402BeaconManifest",
        "entity": AGENT_WALLET,
        "entity_did": f"did:agentcard:scriptmasterlabs:{AGENT_WALLET}",
        "agent": {
            "name": AGENT_NAME,
            "id": AGENT_ID,
            "wallet": AGENT_WALLET,
            "role": "HYBRID",
            "marketplace": "Virtuals ACP",
        },
        "issued_at": _now_ms(),
        "public_key": _public_key_hex(),
        "capabilities": [c["name"] for c in caps],
        "capability_catalog": caps,
        "pricing": {
            "base_rate": str(FLOOR_USD),
            "asset": "USDC",
            "dynamic_bidding": True,
            "floor": str(FLOOR_USD),
        },
        "sla_min_latency_ms": 45,
        "settlement": {
            "payTo": AGENT_WALLET,
            "rails": [
                {
                    "id": "base_usdc",
                    "network": "eip155:8453",
                    "asset": BASE_USDC,
                    "symbol": "USDC",
                    "payTo": AGENT_WALLET,
                    "header": "X-PAYMENT",
                },
                {
                    "id": "robinhood_usdg",
                    "network": "eip155:4663",
                    "asset": USDG_ASSET,
                    "symbol": "USDG",
                    "payTo": AGENT_WALLET,
                    "header": "X-PAYMENT-TX",
                },
                {
                    "id": "solana_usdc",
                    "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                    "asset": SOL_USDC,
                    "symbol": "USDC",
                    "payTo": SOL_PAY_TO,
                    "header": "X-PAYMENT-TX",
                },
                {
                    "id": "xrpl_rlusd",
                    "network": "xrpl",
                    "asset": "RLUSD",
                    "issuer": RLUSD_ISSUER,
                    "payTo": XRPL_PAY_TO,
                    "header": "X-PAYMENT-TX",
                },
            ],
            "refuse_payTo": ["0x4e14"],
        },
        "handshake": {
            "bid_header": "X-x402-Bid",
            "negotiate_header": "X-x402-Negotiate",
            "batch_header": "X-x402-Batch-Size",
            "negotiate_endpoint": f"{base}/beacon/negotiate",
        },
        "discovery": {
            "manifest": f"{base}/.well-known/x402.json",
            "x402_openapi": f"{base}/.well-known/x402",
            "amb": f"{base}/.well-known/amb.json",
            "query": f"{base}/beacon/query",
            "attest": f"{base}/beacon/attest",
            "wedges": [
                f"{base}/x402/gas-tracker",
                f"{base}/x402/crypto-price",
                f"{base}/x402/rwa-aggregates",
            ],
            "x402scan": "https://www.x402scan.com/recipient/0x72330994f379A71542e7bd5a4CF99A9d9743F4aA",
            "homepage": "https://www.scriptmasterlabs.com",
        },
        "protocol": {
            "steps": [
                "GET /.well-known/x402.json",
                "POST /beacon/query with intent",
                "HTTP GET capability route with optional X-x402-Bid",
                "Settle 402 (X-PAYMENT / X-PAYMENT-TX)",
                "POST /beacon/attest quality receipt",
            ]
        },
        "note": (
            "BEACON = discovery+negotiation+attestation layer on live x402. "
            "Payment settle is unchanged multi-rail x402; payTo always 0x7233."
        ),
    }


def query_capabilities(
    *,
    q: str = "",
    max_price: float | None = None,
    max_latency_ms: float | None = None,
    limit: int = 3,
    base_url: str | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    base = (base_url or DEFAULT_BASE).rstrip("/")
    manifest = build_manifest(base_url=base)
    qvec = _tokenize(q or "crypto data feed")
    hits = []
    for cap in manifest.get("capability_catalog") or []:
        cvec = cap.get("vector") or _tokenize(cap.get("name", ""))
        sim = _cosine(qvec, cvec)
        # boost reputation
        rep = float((cap.get("reputation") or {}).get("score") or 0.9)
        price = float((cap.get("pricing") or {}).get("base_rate") or FLOOR_USD)
        lat = float(cap.get("sla_min_latency_ms") or 200)
        if max_price is not None and price > float(max_price) + 1e-12:
            continue
        if max_latency_ms is not None and lat > float(max_latency_ms) + 1e-9:
            continue
        score = sim * 0.75 + rep * 0.25
        # keyword hard boost
        ql = (q or "").lower()
        name = str(cap.get("name") or "")
        if name.replace("_", " ") in ql or name in ql.replace("-", "_"):
            score += 0.35
        for tok in re.findall(r"[a-z0-9]+", ql):
            if tok and tok in name:
                score += 0.05
        hits.append(
            {
                "tool": name,
                "route": cap.get("route"),
                "score": round(min(1.0, score), 4),
                "similarity": round(sim, 4),
                "price": str(price),
                "sla_min_latency_ms": lat,
                "reputation": cap.get("reputation"),
                "pricing": cap.get("pricing"),
                "public_key": manifest.get("public_key"),
            }
        )
    hits.sort(key=lambda x: x["score"], reverse=True)
    top = hits[: max(1, min(int(limit or 3), 25))]
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "beacon_version": BEACON_VERSION,
        "query": q,
        "filters": {"max_price": max_price, "max_latency_ms": max_latency_ms},
        "count": len(top),
        "results": top,
        "routing_ms": round(elapsed_ms, 3),
        "entity": AGENT_WALLET,
        "payTo": AGENT_WALLET,
        "negotiate": {
            "bid_header": "X-x402-Bid",
            "endpoint": f"{base}/beacon/negotiate",
        },
        "manifest": f"{base}/.well-known/x402.json",
    }


def negotiate(
    *,
    tool: str,
    bid: float | None = None,
    batch_size: int = 1,
    base_url: str | None = None,
) -> dict[str, Any]:
    base = (base_url or DEFAULT_BASE).rstrip("/")
    prices = _load_prices()
    # normalize tool name
    t = (tool or "").strip().replace("-", "_")
    if t not in prices:
        # try fuzzy
        for k in prices:
            if k.replace("_", "") == t.replace("_", ""):
                t = k
                break
    if t not in prices:
        return {
            "decision": "Reject",
            "reason": "unknown_tool",
            "tool": tool,
            "header_value": "Reject",
        }
    base_rate = float(prices[t])
    floor = max(FLOOR_USD, base_rate * 0.8)
    ceiling = max(base_rate * 3.0, FLOOR_USD * 5)
    batch = max(1, min(int(batch_size or 1), 10000))
    # batch discount up to 25%
    batch_mult = 1.0
    if batch >= 1000:
        batch_mult = 0.75
    elif batch >= 100:
        batch_mult = 0.85
    elif batch >= 10:
        batch_mult = 0.92
    ask = round(base_rate * batch_mult, 6)
    offer = float(bid) if bid is not None else ask

    if offer + 1e-12 >= ask:
        decision = "Accept"
        final = ask if bid is None else max(ask, min(offer, ceiling))
        # if they bid higher, accept at bid up to ceiling (priority)
        if bid is not None and offer >= ask:
            final = min(offer, ceiling)
        header = "Accept"
    elif offer + 1e-12 >= floor:
        decision = "Counter"
        final = ask
        header = f"Counter: {ask}"
    else:
        decision = "Reject"
        final = ask
        header = "Reject"

    return {
        "beacon_version": BEACON_VERSION,
        "tool": t,
        "route": f"{base}/x402/{_hyphen(t)}",
        "decision": decision,
        "header_value": header,
        "headers": {
            "X-x402-Negotiate": header,
            "X-x402-Ask": str(ask),
            "X-x402-Floor": str(round(floor, 6)),
            "X-x402-Ceiling": str(round(ceiling, 6)),
            "X-x402-Batch-Size": str(batch),
        },
        "base_rate": str(base_rate),
        "ask": str(ask),
        "bid": str(offer) if bid is not None else None,
        "final_price": str(round(final, 6)),
        "batch_size": batch,
        "batch_multiplier": batch_mult,
        "payTo": AGENT_WALLET,
        "asset": BASE_USDC,
        "network": "eip155:8453",
    }


def apply_negotiate_headers(response, tool: str | None = None) -> None:
    """Attach negotiation headers to a Flask 402 response when bid present."""
    try:
        bid_raw = request.headers.get("X-x402-Bid") or request.args.get("bid")
        batch_raw = request.headers.get("X-x402-Batch-Size") or request.args.get("batch") or "1"
        bid = float(bid_raw) if bid_raw not in (None, "") else None
        batch = int(batch_raw)
    except Exception:
        bid = None
        batch = 1
    # infer tool from path
    t = tool
    if not t:
        path = (request.path or "").strip("/")
        if path.startswith("x402/"):
            t = path.split("/", 1)[1].replace("-", "_")
    if not t:
        return
    result = negotiate(tool=t, bid=bid, batch_size=batch)
    for k, v in (result.get("headers") or {}).items():
        response.headers[k] = str(v)
    response.headers["X-BEACON-Version"] = BEACON_VERSION


def attest(
    *,
    tool: str,
    ok: bool = True,
    latency_ms: float = 0.0,
    schema_valid: bool = True,
    agent_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    t = (tool or "").strip().replace("-", "_")
    if not t:
        return {"ok": False, "error": "tool_required"}
    store = _load_store()
    now = _now_ms()
    agent = (agent_id or request.headers.get("X-Agent-Id") or "anon")[:120]
    receipt = {
        "ts": now,
        "tool": t,
        "ok": bool(ok) and bool(schema_valid),
        "latency_ms": float(latency_ms or 0),
        "schema_valid": bool(schema_valid),
        "agent": hashlib.sha256(agent.encode()).hexdigest()[:16],
        "note": (note or "")[:200],
    }
    # sign receipt (HMAC-style content hash — ZK-light placeholder)
    blob = json.dumps(receipt, sort_keys=True).encode()
    receipt["receipt_hash"] = "sha256:" + hashlib.sha256(blob).hexdigest()
    receipt["signature"] = "0x" + hashlib.sha256(
        f"{receipt['receipt_hash']}|{AGENT_WALLET}|beacon-attest".encode()
    ).hexdigest()

    atts = store.setdefault("attestations", [])
    atts.append(receipt)
    if len(atts) > 5000:
        store["attestations"] = atts[-5000:]

    # update reputation
    rep = store.setdefault("reputation", {}).setdefault(
        t, {"score": 0.92, "n": 0, "avg_latency_ms": 420.0, "ok_rate": 0.99, "ok": 0, "fail": 0}
    )
    n = int(rep.get("n", 0)) + 1
    ok_n = int(rep.get("ok", 0)) + (1 if receipt["ok"] else 0)
    fail_n = int(rep.get("fail", 0)) + (0 if receipt["ok"] else 1)
    prev_lat = float(rep.get("avg_latency_ms", 420.0))
    lat = float(latency_ms or prev_lat)
    avg_lat = (prev_lat * (n - 1) + lat) / n if n else lat
    ok_rate = ok_n / n if n else 0.99
    # score: ok_rate heavy + latency term
    lat_term = max(0.0, min(1.0, 1.0 - (avg_lat / 5000.0)))
    score = max(0.05, min(0.999, ok_rate * 0.75 + lat_term * 0.25))
    # slash harder on fail
    if not receipt["ok"]:
        score = max(0.05, score * 0.92)
    rep.update(
        {
            "score": round(score, 4),
            "n": n,
            "ok": ok_n,
            "fail": fail_n,
            "ok_rate": round(ok_rate, 4),
            "avg_latency_ms": round(avg_lat, 1),
            "updated_at": now,
        }
    )
    _save_store(store)
    return {
        "ok": True,
        "receipt": receipt,
        "reputation": reputation_for(t),
        "beacon_version": BEACON_VERSION,
    }


def record_crawl(url: str, ok: bool, note: str = "") -> None:
    store = _load_store()
    crawl = store.setdefault("crawl", [])
    crawl.append({"ts": _now_ms(), "url": url[:300], "ok": ok, "note": note[:200]})
    store["crawl"] = crawl[-500:]
    _save_store(store)


def crawl_well_known(urls: list[str] | None = None) -> dict[str, Any]:
    """Zero-human crawl of peer /.well-known/x402.json (best-effort)."""
    import urllib.request

    seeds = urls or [
        "https://acp-x402-scriptmasterlabs.onrender.com/.well-known/x402.json",
        "https://mcp-x402.onrender.com/.well-known/x402.json",
        "https://www.scriptmasterlabs.com/.well-known/x402.json",
    ]
    found = []
    for u in seeds:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "SML-BEACON-Crawler/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                body = r.read(200000)
            j = json.loads(body.decode("utf-8", "replace"))
            record_crawl(u, True, f"keys={list(j.keys())[:8]}")
            found.append({"url": u, "ok": True, "beacon_version": j.get("beacon_version"), "entity": j.get("entity")})
        except Exception as e:
            record_crawl(u, False, str(e)[:120])
            found.append({"url": u, "ok": False, "error": str(e)[:160]})
    return {"crawled": len(found), "results": found, "beacon_version": BEACON_VERSION}


# ── Flask blueprint ─────────────────────────────────────────────────────────
beacon_bp = Blueprint("beacon", __name__)


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Accept, X-x402-Bid, X-x402-Batch-Size, X-x402-Negotiate, "
        "X-Agent-Id, X-PAYMENT, X-PAYMENT-TX, PAYMENT-SIGNATURE"
    )
    resp.headers["X-BEACON-Version"] = BEACON_VERSION
    return resp


@beacon_bp.route("/.well-known/x402.json", methods=["GET", "OPTIONS"])
@beacon_bp.route("/beacon/manifest", methods=["GET", "OPTIONS"])
def beacon_manifest():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    base = request.host_url.rstrip("/")
    lim = request.args.get("limit", type=int)
    doc = build_manifest(base_url=base, limit=lim)
    resp = jsonify(doc)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "public, max-age=60"
    resp.headers["Link"] = f'<{base}/.well-known/x402.json>; rel="x402-beacon-manifest"'
    return _cors(resp)


@beacon_bp.route("/beacon/query", methods=["GET", "POST", "OPTIONS"])
def beacon_query():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    base = request.host_url.rstrip("/")
    body = request.get_json(silent=True) or {}
    q = (
        body.get("q")
        or body.get("query")
        or body.get("intent")
        or request.args.get("q")
        or request.args.get("query")
        or ""
    )
    max_price = body.get("max_price", request.args.get("max_price", type=float))
    if max_price is not None:
        try:
            max_price = float(max_price)
        except Exception:
            max_price = None
    max_lat = body.get("max_latency_ms", request.args.get("max_latency_ms", type=float))
    if max_lat is not None:
        try:
            max_lat = float(max_lat)
        except Exception:
            max_lat = None
    limit = body.get("limit", request.args.get("limit", 3))
    try:
        limit = int(limit)
    except Exception:
        limit = 3
    doc = query_capabilities(
        q=str(q),
        max_price=max_price,
        max_latency_ms=max_lat,
        limit=limit,
        base_url=base,
    )
    resp = jsonify(doc)
    resp.headers["Cache-Control"] = "no-store"
    return _cors(resp)


@beacon_bp.route("/beacon/negotiate", methods=["GET", "POST", "OPTIONS"])
def beacon_negotiate():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    base = request.host_url.rstrip("/")
    body = request.get_json(silent=True) or {}
    tool = body.get("tool") or body.get("offering") or request.args.get("tool") or ""
    bid = body.get("bid", request.args.get("bid", type=float))
    if bid is not None:
        try:
            bid = float(bid)
        except Exception:
            bid = None
    # also accept header
    if bid is None and request.headers.get("X-x402-Bid"):
        try:
            bid = float(request.headers.get("X-x402-Bid"))
        except Exception:
            bid = None
    batch = body.get("batch_size", request.headers.get("X-x402-Batch-Size") or request.args.get("batch") or 1)
    try:
        batch = int(batch)
    except Exception:
        batch = 1
    doc = negotiate(tool=str(tool), bid=bid, batch_size=batch, base_url=base)
    resp = jsonify(doc)
    for k, v in (doc.get("headers") or {}).items():
        resp.headers[k] = str(v)
    return _cors(resp)


@beacon_bp.route("/beacon/attest", methods=["POST", "OPTIONS"])
def beacon_attest():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    body = request.get_json(silent=True) or {}
    doc = attest(
        tool=str(body.get("tool") or body.get("endpoint") or ""),
        ok=bool(body.get("ok", True)),
        latency_ms=float(body.get("latency_ms") or body.get("latency") or 0),
        schema_valid=bool(body.get("schema_valid", True)),
        agent_id=str(body.get("agent_id") or request.headers.get("X-Agent-Id") or "anon"),
        note=str(body.get("note") or ""),
    )
    status = 200 if doc.get("ok") else 400
    return _cors(jsonify(doc)), status


@beacon_bp.route("/beacon/reputation/<tool>", methods=["GET", "OPTIONS"])
def beacon_reputation(tool: str):
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    t = tool.replace("-", "_")
    return _cors(jsonify(reputation_for(t)))


@beacon_bp.route("/beacon/crawl", methods=["POST", "GET", "OPTIONS"])
def beacon_crawl():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    body = request.get_json(silent=True) or {}
    urls = body.get("urls") if isinstance(body.get("urls"), list) else None
    return _cors(jsonify(crawl_well_known(urls)))


@beacon_bp.route("/beacon/status", methods=["GET", "OPTIONS"])
def beacon_status():
    if request.method == "OPTIONS":
        return _cors(jsonify({})), 204
    base = request.host_url.rstrip("/")
    store = _load_store()
    return _cors(
        jsonify(
            {
                "ok": True,
                "beacon_version": BEACON_VERSION,
                "entity": AGENT_WALLET,
                "manifest": f"{base}/.well-known/x402.json",
                "query": f"{base}/beacon/query",
                "negotiate": f"{base}/beacon/negotiate",
                "attest": f"{base}/beacon/attest",
                "attestation_count": len(store.get("attestations") or []),
                "reputation_tools": len(store.get("reputation") or {}),
                "payTo": AGENT_WALLET,
            }
        )
    )


if __name__ == "__main__":
    print(json.dumps(build_manifest(limit=5), indent=2)[:2500])
    print(json.dumps(query_capabilities(q="sub-50ms gas price feed under 0.001", limit=3), indent=2)[:1500])
