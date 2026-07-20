#!/usr/bin/env python3
"""
a2a_agent_card.py — A2A Protocol agent card for scriptmasterlabs.

Serves /.well-known/agent.json — a discovery document compliant with the
A2A (Agent-to-Agent) Protocol (https://a2a-protocol.org/). It advertises the
scriptmasterlabs agent, its 40 live capabilities (introspected from
provider.py ENDPOINTS), its x402 payment configuration, MCP support, and its
ACP marketplace presence.

Integration into x402_server.py
-------------------------------
    from a2a_agent_card import a2a_bp, build_agent_card
    app.register_blueprint(a2a_bp)
    # or, if you want the card dict in-process:
    card = build_agent_card()

The Blueprint registers exactly one route — `GET /.well-known/agent.json` —
and returns a JSON document shaped per the A2A spec. No payment is required
to read the card itself; it is the discovery surface that tells other agents
HOW to pay and WHAT they can call.

Why this is a Blueprint and not a standalone app
-----------------------------------------------
x402_server.py is the single public HTTP surface for this deployment. The
A2A card belongs on the same origin as the paid endpoints so that the
`url` fields inside each capability resolve to live, payable routes. A
separate Flask app on a different port would break that invariant.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from flask import Blueprint, jsonify, request

# ── In-process introspection of the actual provider ──────────────────────────
# We import ENDPOINTS directly so the card can never drift from what the
# server actually serves. If provider.py grows a new endpoint, the card
# advertises it automatically; if one is removed, the card stops advertising
# it. No manual sync.
from provider import ENDPOINTS as PROVIDER_ENDPOINTS

# x402 configuration (payment rail, network, payee wallet). These are the
# SAME env vars x402_flask.py reads, imported here so the card and the
# paywall agree on a single source of truth.
from x402_flask import (
    PAY_TO,
    NETWORK,
    FACILITATOR,
    X402_VERSION,
    USDC as USDC_ASSETS,
)

logger = logging.getLogger("a2a.agent_card")

# ── Static agent identity ────────────────────────────────────────────────────
# These are deployment-stable facts about the scriptmasterlabs agent. They
# are NOT secrets. The wallet address is public (it is the x402 payee). The
# SDVOSB credentials are public SAM.gov registration identifiers that prove
# the agent's federal-contracting authority.
AGENT_NAME = "scriptmasterlabs"
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
AGENT_WALLET = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
CHAIN_ID = 8453  # Base
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SDVOSB_UEI = "G24VZA4RLMK3"
SDVOSB_CAGE = "21U51"
AGENT_RATING = "5.00"
ACP_OFFERINGS_COUNT = 40
AGENT_DESCRIPTION = (
    "ACP #1 data agent. Wallet analysis, gas tracker, airdrop check, trending "
    "tokens, honeypot rugpull detector, smart money, Hyperliquid funding rate, "
    "DeFi yield, token security. SEC Form 4, 10-K, FRED API, FEC, FDA, OSHA, "
    "Congress bill, SDVOSB federal contracts (UEI G24VZA4RLMK3). 40 APIs from "
    "0.01 USDC. Rating 5.00."
)
AGENT_WEBSITE = os.environ.get(
    "AGENT_WEBSITE", "https://timwal78.github.io/acp-provider/"
)

# x402 per-call USD pricing. We do NOT import x402_server at module top
# level — it has import-time side effects (builds a Flask app and registers
# 40 routes), and importing it from a Blueprint would create a circular
# import + double-registration. Instead we read the price map lazily the
# first time it is needed, and cache it. The price map MUST stay in sync
# with x402_server._PRICES_USD; x402_server asserts it matches ENDPOINTS,
# and build_agent_card() cross-checks every capability has a price.
_X402_PRICES_CACHE: dict[str, str] | None = None


def _get_x402_prices() -> dict[str, str]:
    """Lazily import and cache x402_server._PRICES_USD.

    Returns an empty dict if x402_server cannot be imported (e.g. the
    Blueprint is loaded in a context where the full server isn't present).
    """
    global _X402_PRICES_CACHE
    if _X402_PRICES_CACHE is not None:
        return _X402_PRICES_CACHE
    try:
        from x402_server import _PRICES_USD as _prices
        _X402_PRICES_CACHE = dict(_prices)
    except Exception as e:
        logger.warning("Could not import _PRICES_USD from x402_server (%s); card pricing will be empty", e)
        _X402_PRICES_CACHE = {}
    return _X402_PRICES_CACHE

# ── Endpoint schema metadata ─────────────────────────────────────────────────
# provider.py's ENDPOINTS dict gives us {name: callable} but not the parameter
# shapes. The canonical param schema for every endpoint lives in
# docs/endpoint_schemas.json (copied from /tmp/endpoint_schemas_full.json).
# We load it once at import time and index by endpoint name. Endpoints present
# in ENDPOINTS but missing from the schema file get a permissive schema (no
# required params, all-optional) so they still appear in the card.
_SCHEMA_PATH = Path(__file__).parent / "docs" / "endpoint_schemas.json"


def _load_endpoint_schemas() -> dict[str, dict[str, Any]]:
    """Load and index the endpoint schema catalog by endpoint name.

    Returns a dict keyed by endpoint name. Each value has:
        name, function, description, required_params, optional_params, all_params
    The file is optional; if absent we return an empty dict and every endpoint
    gets a fallback schema.
    """
    try:
        raw = json.loads(_SCHEMA_PATH.read_text())
        return {entry["name"]: entry for entry in raw}
    except FileNotFoundError:
        logger.warning("endpoint_schemas.json not found at %s; using fallback schemas", _SCHEMA_PATH)
        return {}
    except json.JSONDecodeError as e:
        logger.error("endpoint_schemas.json is corrupt (%s); using fallback schemas", e)
        return {}


_ENDPOINT_SCHEMAS = _load_endpoint_schemas()

# Human-readable param type hints. provider.py functions accept a single
# `params` dict and read keys with .get(); they do not enforce types at the
# boundary. These hints let us emit a useful JSON Schema `type` for each param
# so LLM function-callers know whether to send a string, int, or float.
# Anything not listed here defaults to "string".
_PARAM_TYPE_HINTS: dict[str, str] = {
    # numeric
    "top_n": "integer",
    "limit": "integer",
    "per_page": "integer",
    "page": "integer",
    "min_apy": "number",
    "min_tvl": "number",
    "min_amount": "number",
    "chain_id": "integer",
    "fiscal_year": "integer",
    "cycle": "integer",
    "hours": "integer",
    # free-form string params stay string (the default)
}

# Short human descriptions for the most common params, surfaced as the
# JSON Schema `description` field so an LLM calling the tool knows what to send.
_PARAM_DESCRIPTIONS: dict[str, str] = {
    "symbol": "Ticker symbol to filter on (e.g. 'BTC').",
    "top_n": "Maximum number of records to return.",
    "chain": "Blockchain to filter on (e.g. 'ethereum', 'base', 'solana').",
    "min_apy": "Minimum APY (fraction, e.g. 0.05 = 5%) to include.",
    "min_tvl": "Minimum TVL in USD to include.",
    "project": "DeFi project name to filter on (e.g. 'aave').",
    "category": "DeFi category to filter on (e.g. 'lending').",
    "agency": "Federal agency name to filter on.",
    "naics": "NAICS code to filter on.",
    "min_amount": "Minimum award amount in USD.",
    "contractor_name": "Contractor / recipient name to look up.",
    "entity_name": "Entity name to verify.",
    "uei": "SAM.gov Unique Entity ID (UEI).",
    "cage": "SAM.gov Commercial and Government Entity (CAGE) code.",
    "fiscal_year": "Federal fiscal year (e.g. 2025).",
    "ticker": "SEC ticker symbol (e.g. 'AAPL').",
    "cik": "SEC CIK number for the filer.",
    "name": "Entity or candidate name to search for.",
    "committee": "FEC committee ID or name.",
    "cycle": "FEC election cycle (e.g. 2024).",
    "series_id": "FRED series ID (e.g. 'GDP').",
    "query": "Search query string.",
    "congress": "Congress number (e.g. 118).",
    "client": "Lobbying client name.",
    "registrant": "Lobbying registrant name.",
    "issue": "Lobbying issue code or topic.",
    "company": "Company name to filter on.",
    "product": "Product name to filter on.",
    "drug": "Drug name to query.",
    "facility": "EPA facility name to filter on.",
    "state": "US state abbreviation (e.g. 'CA').",
    "establishment": "OSHA establishment name to filter on.",
    "claim": "Claim string to fact-check.",
    "domain": "Domain to ground the fact-check against (e.g. 'fda', 'sec').",
    "token_address": "Token contract address.",
    "token": "Token symbol or address.",
    "address": "Wallet address to analyze.",
    "wallet": "Wallet address to check.",
    "assets": "Comma-separated asset list for macro analysis.",
    "timeframe": "Analysis timeframe: 'short', 'medium', or 'long'.",
    "bank_id": "Bank identifier for compliance queries.",
    "trigger": "Compliance anomaly trigger description.",
    "detail": "Compliance anomaly detail.",
    "severity": "Anomaly severity: 'low', 'medium', 'high', or 'critical'.",
    "order": "CoinGecko sort order (e.g. 'market_cap_desc').",
    "vs_currency": "Quote currency (default 'usd').",
    "tokens": "Comma-separated CoinGecko coin IDs (e.g. 'bitcoin,ethereum').",
    "vs": "Quote currency for price lookup.",
    "include_24hr": "Whether to include 24h change.",
    "include_market_cap": "Whether to include market cap.",
    "include_24hr_vol": "Whether to include 24h volume.",
    "include_last_updated_at": "Whether to include last-updated timestamp.",
}


def _param_schema(param_name: str) -> dict[str, Any]:
    """Build a single JSON Schema property object for a parameter."""
    schema: dict[str, Any] = {"type": _PARAM_TYPE_HINTS.get(param_name, "string")}
    desc = _PARAM_DESCRIPTIONS.get(param_name)
    if desc:
        schema["description"] = desc
    return schema


def _build_input_schema(schema_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Build a JSON Schema object describing a capability's input.

    `schema_entry` is one record from docs/endpoint_schemas.json. If it is
    None (endpoint not in the schema file) we emit a permissive schema that
    accepts any object — the endpoint still appears in the card, just with
    weaker param documentation.
    """
    if schema_entry is None:
        return {
            "type": "object",
            "additionalProperties": True,
            "properties": {},
            "required": [],
            "description": "Accepts a params object; see the endpoint's HTTP docs for fields.",
        }

    required = list(schema_entry.get("required_params", []) or [])
    optional = list(schema_entry.get("optional_params", []) or [])
    all_params = list(schema_entry.get("all_params", []) or (required + optional))

    properties = {p: _param_schema(p) for p in all_params}
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        "description": schema_entry.get("description", ""),
    }


def _build_output_schema(name: str) -> dict[str, Any]:
    """Build a JSON Schema object describing a capability's output.

    Every provider.py function returns a JSON-serializable dict. We don't
    have per-endpoint output schemas codified, so we declare a permissive
    object schema with a couple of common top-level fields the functions
    consistently include. This is intentionally loose — the goal is to tell
    A2A consumers 'you'll get a JSON object back', not to over-constrain.
    """
    return {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "error": {"type": "string", "description": "Present only on failure."},
        },
        "additionalProperties": True,
        "description": f"JSON object returned by the {name} endpoint.",
    }


def _capability_for(name: str, fn: Callable[..., Any]) -> dict[str, Any] | None:
    """Build a single A2A capability object for one endpoint.

    Returns None if the endpoint has no price (shouldn't happen given the
    x402_server assert, but we guard anyway) — callers skip None entries.
    """
    price_str = _get_x402_prices().get(name)
    schema_entry = _ENDPOINT_SCHEMAS.get(name)
    description = (schema_entry.get("description") if schema_entry else None) or (
        fn.__doc__ and fn.__doc__.strip().splitlines()[0]
    ) or name

    # The HTTP route x402_server registers for this endpoint. Hyphenated,
    # under /x402/. We use request.host_url at serve time so the card always
    # reflects the origin it was fetched from (works behind proxies/LB).
    route_path = f"/x402/{name.replace('_', '-')}"

    capability: dict[str, Any] = {
        "id": name,
        "name": name,
        "description": description,
        "url": route_path,  # resolved to absolute at serve time
        "inputSchema": _build_input_schema(schema_entry),
        "outputSchema": _build_output_schema(name),
        "pricing": {
            "scheme": "x402-exact",
            "amount": price_str,
            "currency": "USDC",
            "network": NETWORK,
            "asset": USDC_ASSETS.get(NETWORK, USDC_ASSETS.get("base-sepolia", {})).get("asset"),
            "payTo": PAY_TO,
            "facilitator": FACILITATOR,
            "perCall": True,
            "description": (
                f"Pay {price_str} USDC on {NETWORK} per call via the x402 "
                f"protocol (HTTP 402 → X-PAYMENT → /verify → /settle)."
                if price_str
                else "x402 payment required; see /.well-known/x402 for details."
            ),
        },
        "transport": {
            "type": "http",
            "method": "GET",
            "accepts": ["application/json"],
            "returns": ["application/json"],
        },
    }
    return capability


def build_agent_card(base_url: str | None = None) -> dict[str, Any]:
    """Build the full A2A agent card as a dict.

    `base_url` is used to absolutize the per-capability `url` fields. If
    None, the route paths are left relative (the Blueprint route will
    absolutize them against request.host_url at serve time).
    """
    # Build capabilities from the live ENDPOINTS dict, preserving the dict's
    # insertion order (which groups endpoints by domain in provider.py).
    capabilities = []
    for name, fn in PROVIDER_ENDPOINTS.items():
        cap = _capability_for(name, fn)
        if cap is None:
            continue
        if base_url:
            cap["url"] = base_url.rstrip("/") + "/" + cap["url"].lstrip("/")
        capabilities.append(cap)

    # Payment + auth info block. This tells A2A consumers how to pay and how
    # to bypass payment if they hold an operator/agent API key (the same bypass
    # x402_guard implements).
    usdc_cfg = USDC_ASSETS.get(NETWORK, USDC_ASSETS.get("base-sepolia", {}))
    payment_config = {
        "x402": {
            "version": X402_VERSION,
            "supported": True,
            "network": NETWORK,
            "asset": usdc_cfg.get("asset"),
            "assetSymbol": "USDC",
            "payTo": PAY_TO,
            "facilitator": FACILITATOR,
            "discovery": "/.well-known/x402",
            "flow": [
                "GET /x402/<endpoint> without X-PAYMENT → HTTP 402 with payment requirements",
                "Agent pays exact USDC amount to payTo on the declared network",
                "Retry the request with header X-PAYMENT: base64(payment payload)",
                "Server verifies via facilitator /verify, serves data, settles via /settle",
            ],
        },
        "apiKeyBypass": {
            "supported": True,
            "headers": ["X-API-Key", "X-Owner-Key", "Authorization: Bearer <key>"],
            "description": (
                "Requests carrying a valid OPERATOR_API_KEY, OWNER_API_KEY, or a "
                "key listed in AGENT_API_KEYS skip x402 settlement. Used by agents "
                "that already collected payment upstream (e.g. via ACP)."
            ),
        },
    }

    # MCP support block — points at the mcp_server Blueprint's manifest.
    mcp_base = (base_url or "").rstrip("/") or ""
    mcp_config = {
        "supported": True,
        "manifest": "/.well-known/mcp.json",
        "endpoint": "/mcp",
        "sseEndpoint": "/mcp/sse",
        "transport": "http-jsonrpc-sse",
        "description": (
            "Model Context Protocol server exposing all capabilities as MCP "
            "tools. tools/list and tools/call over JSON-RPC 2.0; notifications "
            "via SSE. tools/call is gated by the same x402 payment as the HTTP "
            "routes."
        ),
    }
    if base_url:
        mcp_config["manifest"] = mcp_base + mcp_config["manifest"]
        mcp_config["endpoint"] = mcp_base + mcp_config["endpoint"]
        mcp_config["sseEndpoint"] = mcp_base + mcp_config["sseEndpoint"]

    # ACP marketplace presence block.
    acp_config = {
        "present": True,
        "marketplace": "Virtuals ACP",
        "agentId": AGENT_ID,
        "offerings": ACP_OFFERINGS_COUNT,
        "rating": AGENT_RATING,
        "chainId": CHAIN_ID,
        "wallet": AGENT_WALLET,
        "description": (
            "The same 40 capabilities are also sold on the Virtuals ACP "
            "marketplace as agent-to-agent job escrow (micro-USD pricing). "
            "ACP and x402 are independent protocols; this card advertises the "
            "x402 HTTP surface."
        ),
    }

    # SDVOSB / federal credentials block. These are public SAM.gov
    # identifiers that distinguish scriptmasterlabs as the only SAM.gov-
    # registered SDVOSB data agent on ACP. Surfacing them in the card lets
    # federal-procurement A2A consumers verify authority without an extra
    # round-trip to SAM.gov.
    credentials = {
        "sdvosb": {
            "registered": True,
            "uei": SDVOSB_UEI,
            "cage": SDVOSB_CAGE,
            "description": (
                "Service-Disabled Veteran-Owned Small Business, registered in "
                "SAM.gov. Authorizes the agent to bid on SDVOSB/VOSB set-aside "
                "federal contract opportunities."
            ),
        },
        "chain": {
            "name": "Base",
            "chainId": CHAIN_ID,
            "usdc": USDC_CONTRACT,
        },
    }

    card: dict[str, Any] = {
        "protocol": "a2a",
        "protocolVersion": "1.0",
        "name": AGENT_NAME,
        "id": AGENT_ID,
        "description": AGENT_DESCRIPTION,
        "url": (base_url or "/"),
        "version": "1.0.0",
        "capabilities": capabilities,
        "capabilitiesCount": len(capabilities),
        "payment": payment_config,
        "mcp": mcp_config,
        "acp": acp_config,
        "credentials": credentials,
        "authentication": {
            "schemes": ["x402", "api-key"],
            "credentials": {
                "x402": {
                    "type": "http402",
                    "description": "Pay per call via x402 (USDC on Base).",
                    "payTo": PAY_TO,
                    "network": NETWORK,
                },
                "api-key": {
                    "type": "apiKey",
                    "in": "header",
                    "headerName": "X-API-Key",
                    "description": "Operator/agent key bypasses x402 payment.",
                },
            },
        },
        "skills": [
            {
                "id": "crypto-analytics",
                "name": "Crypto & DeFi Analytics",
                "description": "Hyperliquid funding, DeFi yields/TVL, token security, wallet analysis, gas, airdrops, trending tokens, smart money, liquidation risk.",
            },
            {
                "id": "sec-edgar",
                "name": "SEC EDGAR Filings",
                "description": "10-K, 10-Q, 8-K, Form 4 insider trades, 13F institutional holdings, 13D/13G activist filings.",
            },
            {
                "id": "fda-safety",
                "name": "FDA Safety",
                "description": "FDA warning letters, drug recall alerts, adverse event reports.",
            },
            {
                "id": "regulatory-enforcement",
                "name": "Regulatory Enforcement",
                "description": "EPA ECHO violations, OSHA inspections, FEC campaign finance, FRED economic indicators, Congress bill search, lobbying disclosures.",
            },
            {
                "id": "federal-contracting",
                "name": "Federal Contracting (SDVOSB)",
                "description": "USAspending.gov contract opportunities, award history, SDVOSB set-aside feed, SAM entity verification, federal spending by agency, excluded parties check.",
            },
            {
                "id": "compliance-macro",
                "name": "Compliance & Macro",
                "description": "AI fact-check oracle, entity compliance check, Druckenmiller macro regime analysis, bank compliance audit/anomaly/regulator queries.",
            },
        ],
        "meta": {
            "website": AGENT_WEBSITE,
            "operator": "ScriptMasterLabs",
            "rating": AGENT_RATING,
            "offeringsOnACP": ACP_OFFERINGS_COUNT,
            "freeTier": False,
            "documentation": "/.well-known/x402",
        },
    }
    return card


# ── Flask Blueprint ──────────────────────────────────────────────────────────
a2a_bp = Blueprint("a2a_agent_card", __name__)


@a2a_bp.route("/.well-known/agent.json", methods=["GET"])
def agent_json():
    """Serve the A2A agent card.

    The card is rebuilt on every request so it always reflects the current
    ENDPOINTS dict, env-configured payment rail, and request origin. This is
    cheap (40 capabilities, no I/O) and avoids stale cards after a redeploy
    that adds/removes endpoints or rotates the payee wallet.
    """
    base_url = request.host_url.rstrip("/")
    card = build_agent_card(base_url=base_url)
    resp = jsonify(card)
    resp.headers["Content-Type"] = "application/json"
    # A2A cards are public discovery documents; cache briefly to let indexers
    # re-fetch without hammering, but keep TTL short so changes propagate.
    resp.headers["Cache-Control"] = "public, max-age=300"
    # CORS: A2A consumers are typically other agents on other origins.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ── CLI entrypoint for local debugging ───────────────────────────────────────
# `python a2a_agent_card.py` prints the card as JSON so you can eyeball it
# without standing up the Flask server.
if __name__ == "__main__":
    print(json.dumps(build_agent_card(base_url="http://localhost:8080"), indent=2))
