"""
vendos_bp.py — VendOS Marketplace Blueprint
Mounts on acp-x402 Flask service to provide the VendOS listing API.

Routes:
  GET  /marketplace/listings  → JSON list of all listings
  POST /marketplace/list      → create listing (402 challenge first, then accept with X-Payment-Proof)
  GET  /marketplace/stats     → count, hosts, last_updated
  GET  /vendos/status         → health + listing count
"""
import json
import os
import uuid
import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

vendos_bp = Blueprint("vendos", __name__)

# ── Storage ──────────────────────────────────────────────────────────────────
_LISTINGS_FILE = "/tmp/vendos_listings.json"
_PAY_TO = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
_BASE_HOST = "https://acp-x402-scriptmasterlabs.onrender.com"

# In-memory store (survives hot reloads; resets on worker restart, file cache used for boot)
_listings: list = []
_last_updated: str = datetime.now(timezone.utc).isoformat()

# ── Seed data ─────────────────────────────────────────────────────────────────
_SML_SEED = [
    {
        "name": "Crypto Price",
        "description": "Real-time crypto prices for any CoinGecko id. Returns price, market cap, 24h change.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/crypto-price",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["crypto", "price", "market"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "Gas Tracker",
        "description": "Live gas prices for Ethereum and other EVM chains.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/gas-tracker",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["gas", "ethereum", "evm"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "Web Search",
        "description": "AI-powered web search — returns structured results for any query.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/web-search",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["search", "web", "ai"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "Wallet Analyzer",
        "description": "Deep on-chain wallet analysis: token balances, DeFi positions, activity.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/wallet-analyzer",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["wallet", "onchain", "defi"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "DeFi Yield Rates",
        "description": "Current DeFi lending and liquidity pool yield rates across protocols.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/defi-yield-rates",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["defi", "yield", "lending"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "RWA Assets",
        "description": "Real-world asset tokenization index — treasuries, commodities, real estate on-chain.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/rwa-assets",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["rwa", "tokenized", "treasury"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "SEC 10-K Annual Filing",
        "description": "Pull latest SEC 10-K annual filings for any public company via EDGAR.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/sec-10-k-annual-filing",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["sec", "edgar", "compliance"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "SAM Opportunities",
        "description": "Live federal contract opportunities from SAM.gov. SDVOSB set-aside support.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/sam-opportunities",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["government", "federal", "contracts", "sam"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "Rug Pull Detector",
        "description": "Token honeypot + rug pull risk scoring. Returns risk flags and contract analysis.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/rugpull-detector",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["security", "defi", "token", "risk"],
        "is_scriptmasterlabs": True,
    },
    {
        "name": "FRED Economic Indicators",
        "description": "Federal Reserve FRED macroeconomic data: CPI, GDP, unemployment, rates.",
        "base_url": _BASE_HOST,
        "endpoint": "/x402/fred-economic-indicators",
        "cost": "0.001",
        "currency": "USDC",
        "network": "base",
        "pay_to": _PAY_TO,
        "method": "GET",
        "tags": ["macro", "economics", "fred", "fed"],
        "is_scriptmasterlabs": True,
    },
]


def _load_from_file() -> list:
    """Load listings from file cache. Returns empty list on any error."""
    try:
        if os.path.exists(_LISTINGS_FILE):
            with open(_LISTINGS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        log.warning("vendos: failed to load listings file: %s", e)
    return []


def _save_to_file(listings: list) -> None:
    """Persist listings to file cache. Fire-and-forget."""
    try:
        with open(_LISTINGS_FILE, "w") as f:
            json.dump(listings, f, indent=2)
    except Exception as e:
        log.warning("vendos: failed to save listings file: %s", e)


def _init_listings() -> None:
    """Seed listings on startup. Merge file cache with SML seed."""
    global _listings, _last_updated
    cached = _load_from_file()
    # Deduplicate by endpoint: prefer cached version for community listings
    by_ep: dict = {}
    for item in cached:
        ep_key = (item.get("base_url", ""), item.get("endpoint", ""))
        by_ep[ep_key] = item
    # Always ensure SML seed listings are present
    for seed in _SML_SEED:
        ep_key = (seed["base_url"], seed["endpoint"])
        if ep_key not in by_ep:
            by_ep[ep_key] = {
                **seed,
                "id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
    _listings = list(by_ep.values())
    _last_updated = datetime.now(timezone.utc).isoformat()
    _save_to_file(_listings)


# Initialize on module load
_init_listings()


# ── 402 Challenge ─────────────────────────────────────────────────────────────
_LIST_CHALLENGE = {
    "accepts": [
        {
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "50000",
            "payTo": _PAY_TO,
        }
    ]
}


# ── Routes ────────────────────────────────────────────────────────────────────

@vendos_bp.route("/marketplace/listings", methods=["GET"])
def marketplace_listings():
    """Return all listings as JSON."""
    global _listings
    return jsonify({
        "ok": True,
        "count": len(_listings),
        "listings": _listings,
        "last_updated": _last_updated,
        "paywall": "0.05 USDC to list · agents pay sellers direct",
    })


@vendos_bp.route("/marketplace/list", methods=["POST"])
def marketplace_list():
    """
    POST /marketplace/list
    First call (no X-Payment-Proof) → 402 challenge.
    Second call (X-Payment-Proof = confirmed Base tx hash paying the 0.05 USDC
    listing fee to _PAY_TO) → verified on-chain, store listing, return 200.
    """
    global _listings, _last_updated

    payment_proof = request.headers.get("X-Payment-Proof")

    if not payment_proof:
        # Return 402 challenge
        resp = jsonify(_LIST_CHALLENGE)
        resp.status_code = 402
        resp.headers["X-402-Version"] = "1"
        resp.headers["X-402-Network"] = "eip155:8453"
        return resp

    # Verify the payment actually happened on-chain before accepting a free-form
    # listing. Previously this only checked the header was non-empty — any
    # string at all (even "x") satisfied `if not payment_proof`, so the
    # advertised "0.05 USDC to list" fee was never actually collected or
    # checked. _LIST_CHALLENGE's shape (bare {network, asset, amount, payTo})
    # matches _verify_base_usdc_tx's raw on-chain-tx-hash verification, the
    # same already-replay-protected function the sovereign rail in
    # x402_flask.py uses for real Base USDC payments — reused here rather
    # than re-derived.
    try:
        from x402_flask import _verify_base_usdc_tx
        list_min_units = int(_LIST_CHALLENGE["accepts"][0]["amount"])
        verify = _verify_base_usdc_tx(payment_proof, _PAY_TO, list_min_units)
    except Exception as e:
        log.error("vendos: payment verification import/call failed: %s", e)
        return jsonify({"error": "payment_verification_unavailable", "detail": str(e)}), 503
    if not verify.get("ok"):
        return jsonify({"error": "invalid_payment_proof", "reason": verify.get("error")}), 402

    # Payment verified on-chain — accept and store listing
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    endpoint = (body.get("endpoint") or "").strip()
    pay_to = (body.get("pay_to") or "").strip()

    if not name or not base_url or not endpoint or not pay_to:
        return jsonify({"error": "name, base_url, endpoint, and pay_to are required"}), 400

    listing = {
        "id": str(uuid.uuid4()),
        "name": name,
        "description": (body.get("description") or "").strip(),
        "base_url": base_url,
        "endpoint": endpoint,
        "cost": str(body.get("cost") or "0.001"),
        "currency": str(body.get("currency") or "USDC"),
        "network": str(body.get("network") or "base"),
        "pay_to": pay_to,
        "method": str(body.get("method") or "GET").upper(),
        "tags": body.get("tags") or [],
        "is_scriptmasterlabs": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payment_proof": payment_proof[:64],  # store truncated proof reference
    }

    _listings.append(listing)
    _last_updated = datetime.now(timezone.utc).isoformat()
    _save_to_file(_listings)

    log.info("vendos: new listing added — %s at %s%s", name, base_url, endpoint)

    return jsonify({
        "ok": True,
        "listing": listing,
        "message": f"Listed '{name}'. Fee recorded. Agents will discover your store at {base_url}{endpoint}.",
    })


@vendos_bp.route("/marketplace/stats", methods=["GET"])
def marketplace_stats():
    """Return aggregate stats."""
    hosts = list({
        item.get("base_url", "").replace("https://", "").split("/")[0]
        for item in _listings
        if item.get("base_url")
    })
    return jsonify({
        "ok": True,
        "count": len(_listings),
        "hosts": len(hosts),
        "host_list": sorted(hosts),
        "last_updated": _last_updated,
        "sml_count": sum(1 for l in _listings if l.get("is_scriptmasterlabs")),
        "community_count": sum(1 for l in _listings if not l.get("is_scriptmasterlabs")),
    })


@vendos_bp.route("/vendos/status", methods=["GET"])
def vendos_status():
    """Health check + listing count."""
    return jsonify({
        "ok": True,
        "service": "VendOS Marketplace",
        "version": "1.0.0",
        "host": _BASE_HOST,
        "listing_count": len(_listings),
        "last_updated": _last_updated,
        "list_fee_usdc": "0.05",
        "list_fee_atomic": "50000",
        "payment_asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "eip155:8453",
        "pay_to": _PAY_TO,
        "routes": [
            "GET /marketplace/listings",
            "POST /marketplace/list",
            "GET /marketplace/stats",
            "GET /vendos/status",
        ],
    })
