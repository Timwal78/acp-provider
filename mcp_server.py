#!/usr/bin/env python3
"""
mcp_server.py — Model Context Protocol (MCP) server for scriptmasterlabs.

Exposes all 40 scriptmasterlabs capabilities as MCP tools, consumable by any
MCP-compatible client (Claude Desktop, Cursor, Continue, custom agents) over
JSON-RPC 2.0. Implements three transports on one Flask Blueprint:

    GET  /.well-known/mcp.json   — MCP server manifest (tool catalog, no payment)
    POST /mcp                     — JSON-RPC 2.0 endpoint (tools/list, tools/call)
    GET  /mcp/sse                 — SSE stream for MCP notifications

Why MCP alongside A2A + x402
----------------------------
A2A is agent-to-agent discovery (who can do what, how to pay). x402 is the
payment rail (HTTP 402 → pay → settle). MCP is the *tool-calling* surface an
LLM uses to actually invoke a capability. A single capability — say
`perp_funding_aggregator` — is advertised in the A2A card, priced via x402,
and *invoked* through MCP `tools/call`. All three layers front the same
provider.py functions.

Payment on tools/call
---------------------
`tools/list` and the manifest are free (they're discovery — no different from
the A2A card). `tools/call` is gated by the SAME `x402_guard` that protects
the HTTP routes, so an MCP client pays exactly what a direct HTTP client
pays: the per-call USDC price from x402_server._PRICES_USD. The guard is
applied as a per-tool price check inside the JSON-RPC handler rather than as
a Flask route decorator, because MCP dispatches many tools through one
route.

Integration into x402_server.py
-------------------------------
    from mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)

The Blueprint registers the three routes above and is otherwise self-contained.
It imports ENDPOINTS from provider.py and the price map from x402_server.py,
so it stays in sync with both automatically.

MCP spec reference: https://modelcontextprotocol.io/specification
JSON-RPC 2.0 spec:  https://www.jsonrpc.org/specification
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request, stream_with_context

# Live endpoint registry — the single source of truth for what tools exist.
from provider import ENDPOINTS as PROVIDER_ENDPOINTS

# x402 payment rail config. We reuse the SAME env vars x402_flask.py reads so
# there is one payment configuration across HTTP, A2A, and MCP surfaces.
from x402_flask import (
    PAY_TO,
    NETWORK,
    FACILITATOR,
    X402_VERSION,
    USDC as USDC_ASSETS,
    x402_guard,
    _payment_requirements,
    _402 as _x402_response,
    _facilitator,
)

# Per-call USD pricing. We do NOT import x402_server at module top level —
# it has import-time side effects (builds a Flask app and registers 40
# routes), and importing it from a Blueprint would create a circular import
# + double-registration. We read the price map lazily and cache it. The
# price map MUST stay in sync with x402_server._PRICES_USD.
_X402_PRICES_CACHE: dict[str, str] | None = None
_COERCE_PARAMS_FN: Callable[[dict], dict] | None = None


def _get_x402_prices() -> dict[str, str]:
    """Lazily import and cache x402_server._PRICES_USD."""
    global _X402_PRICES_CACHE
    if _X402_PRICES_CACHE is not None:
        return _X402_PRICES_CACHE
    try:
        from x402_server import _PRICES_USD as _prices, _coerce_params as _coerce
        _X402_PRICES_CACHE = dict(_prices)
        _set_coerce_fn(_coerce)
    except Exception as e:
        logging.getLogger("mcp").warning(
            "Could not import _PRICES_USD / _coerce_params from x402_server (%s); "
            "MCP tools/call will not be payable.", e,
        )
        _X402_PRICES_CACHE = {}
    return _X402_PRICES_CACHE


def _set_coerce_fn(fn: Callable[[dict], dict]) -> None:
    global _COERCE_PARAMS_FN
    _COERCE_PARAMS_FN = fn


def _coerce_params(raw: dict) -> dict:
    """Coerce tool args to typed values. Falls back to a stdlib-only coercer
    if x402_server._coerce_params is unavailable."""
    if _COERCE_PARAMS_FN is not None:
        return _COERCE_PARAMS_FN(raw)
    # Minimal fallback: numeric strings → int/float, else string.
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, str) and v.isdigit():
            out[k] = int(v)
        else:
            try:
                out[k] = float(v) if isinstance(v, str) else v
            except (ValueError, TypeError):
                out[k] = v
    return out

logger = logging.getLogger("mcp")

# ── Agent identity (shared with a2a_agent_card.py — kept in sync manually) ──
AGENT_NAME = "scriptmasterlabs"
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
SERVER_NAME = "scriptmasterlabs-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"  # MCP protocol version this server speaks

# ── Endpoint schema metadata (same catalog a2a_agent_card.py uses) ───────────
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "docs" / "endpoint_schemas.json"


def _load_endpoint_schemas() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_SCHEMA_PATH.read_text())
        return {entry["name"]: entry for entry in raw}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error("endpoint_schemas.json corrupt: %s", e)
        return {}


_ENDPOINT_SCHEMAS = _load_endpoint_schemas()

# Param type hints + descriptions. Duplicated from a2a_agent_card.py to keep
# this file independently importable; keeping them in sync is a small price
# for not creating a cross-module dependency for two small dicts. If this
# grows, factor into a shared schemas.py.
_PARAM_TYPE_HINTS: dict[str, str] = {
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
}

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
}


def _param_schema(param_name: str) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _PARAM_TYPE_HINTS.get(param_name, "string")}
    desc = _PARAM_DESCRIPTIONS.get(param_name)
    if desc:
        schema["description"] = desc
    return schema


def _build_input_schema(schema_entry: dict[str, Any] | None) -> dict[str, Any]:
    """Build a JSON Schema for an MCP tool's input.

    MCP tool inputSchema follows JSON Schema convention: a top-level object
    with `properties` and `required`. This is what an LLM function-caller
    inspects to decide what arguments to produce.
    """
    if schema_entry is None:
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": True,
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
    }


def _tool_for(name: str, fn: Callable[..., Any]) -> dict[str, Any]:
    """Build a single MCP tool descriptor for one endpoint."""
    schema_entry = _ENDPOINT_SCHEMAS.get(name)
    description = (schema_entry.get("description") if schema_entry else None) or (
        fn.__doc__ and fn.__doc__.strip().splitlines()[0]
    ) or name
    price_str = _get_x402_prices().get(name)

    # Enrich the description with pricing + the HTTP route so an LLM reading
    # the tool list knows it's paid and where the REST equivalent lives.
    price_note = f" (x402: {price_str} USDC per call)" if price_str else ""
    http_route = f"/x402/{name.replace('_', '-')}"
    full_description = (
        f"{description}{price_note}\n\n"
        f"HTTP equivalent: GET {http_route}\n"
        f"Payment: x402 protocol — send X-PAYMENT header with base64 payment "
        f"payload, or X-API-Key / X-Owner-Key / Bearer token to bypass."
    )

    return {
        "name": name,
        "description": full_description,
        "inputSchema": _build_input_schema(schema_entry),
        # MCP annotations hint to the client how the tool should be presented.
        "annotations": {
            "title": name.replace("_", " ").title(),
            "category": _category_for(name),
            "pricing": {
                "amount": price_str,
                "currency": "USDC",
                "network": NETWORK,
                "perCall": True,
            },
            "httpRoute": http_route,
        },
    }


def _category_for(name: str) -> str:
    """Rough category bucket for UI grouping in MCP clients."""
    if name.startswith("sec_"):
        return "SEC EDGAR"
    if name.startswith("fda_"):
        return "FDA Safety"
    if name.startswith("compliance_"):
        return "Compliance"
    if name in {
        "federal_contract_opportunities",
        "federal_award_history",
        "sdvosb_setaside_feed",
        "sam_entity_verification",
        "federal_spending_by_agency",
        "excluded_parties_check",
        "entity_compliance_check",
    }:
        return "Federal Contracting"
    if name in {
        "fec_campaign_finance",
        "fred_economic_indicators",
        "congressional_bills_search",
        "lobbying_disclosures",
        "epa_environmental_violations",
        "osha_inspection_records",
    }:
        return "Regulatory"
    if name == "druckenmiller_macro_regime_analysis":
        return "Macro"
    if name == "ai_fact_check":
        return "AI"
    return "Crypto & DeFi"


def _all_tools() -> list[dict[str, Any]]:
    """Build the full MCP tool list from the live ENDPOINTS dict."""
    return [_tool_for(name, fn) for name, fn in PROVIDER_ENDPOINTS.items()]


# ── Payment gate for tools/call ──────────────────────────────────────────────
def _check_tool_payment(name: str) -> tuple[bool, Any]:
    """Enforce x402 payment for a tools/call invocation.

    Mirrors the logic in x402_flask.x402_guard but adapted for the JSON-RPC
    dispatch context (we can't use the decorator because tools/call routes
    many tools through one Flask endpoint).

    Returns (allowed, response_payload):
      - allowed=True  → caller may proceed; response_payload is None
      - allowed=False → caller must NOT proceed; response_payload is a JSON-RPC
                        error dict the handler returns directly. If payment is
                        required, the error includes the x402 payment
                        requirements so the client can pay and retry.

    Bypass logic matches x402_guard exactly: OPERATOR_API_KEY, OWNER_API_KEY,
    AGENT_API_KEYS, via X-API-Key / X-Owner-Key / Authorization: Bearer.
    """
    price_str = _get_x402_prices().get(name)
    if not price_str:
        # No price mapped — treat as free (shouldn't happen given x402_server's
        # assert, but be permissive rather than 500'ing the RPC call).
        return True, None

    if not PAY_TO:
        return False, _rpc_error(
            None,
            code=-32001,
            message="ERR_PAYMENT_NOT_CONFIGURED",
            data={"detail": "X402_PAY_TO is not set on this deployment."},
        )

    # Operator/agent key bypass — identical to x402_guard.
    auth_header = request.headers.get("Authorization", "")
    bearer_key = auth_header.split("Bearer ")[-1].strip() if "Bearer " in auth_header else ""
    passed_key = (
        request.headers.get("X-Owner-Key")
        or request.headers.get("X-API-Key")
        or bearer_key
    )
    agent_keys = [k.strip() for k in os.environ.get("AGENT_API_KEYS", "").split(",") if k.strip()]
    valid_keys = [k for k in [os.environ.get("OPERATOR_API_KEY"), os.environ.get("OWNER_API_KEY")] if k] + agent_keys
    if passed_key and passed_key in valid_keys:
        return True, None

    # Look for an X-PAYMENT header (base64 payment payload). If present,
    # verify via the facilitator. If valid, we let the call through and settle
    # after. If absent or invalid, return a JSON-RPC error carrying the x402
    # payment requirements so the client can pay and retry.
    resource = request.base_url + f"/{name}"
    reqs = _payment_requirements(
        price_str,
        f"scriptmasterlabs — {name.replace('_', ' ')}",
        resource,
    )

    header = request.headers.get("X-PAYMENT")
    if not header:
        return False, _rpc_error(
            None,
            code=-32001,
            message="payment required",
            data={"x402": reqs, "flow": "retry with X-PAYMENT header after paying"},
        )

    import base64
    try:
        payment_payload = json.loads(base64.b64decode(header))
    except Exception as e:
        return False, _rpc_error(
            None,
            code=-32002,
            message="malformed X-PAYMENT header",
            data={"detail": str(e)},
        )

    verify = _facilitator("/verify", payment_payload, reqs)
    if not verify.get("isValid", False):
        return False, _rpc_error(
            None,
            code=-32003,
            message=f"invalid payment: {verify.get('invalidReason', 'unknown')}",
            data={"x402": reqs},
        )

    # Stash for post-call settle. We return allowed=True; the handler will
    # call _settle_after_call() once the tool has run.
    request._mcp_payment_payload = payment_payload  # type: ignore[attr-defined]
    request._mcp_payment_reqs = reqs  # type: ignore[attr-defined]
    return True, None

def _settle_after_call(name: str) -> None:
    """Settle the x402 payment after a successful tools/call. Best-effort."""
    payload = getattr(request, "_mcp_payment_payload", None)
    reqs = getattr(request, "_mcp_payment_reqs", None)
    if not payload or not reqs:
        return
    try:
        settle = _facilitator("/settle", payload, reqs)
        if settle.get("success"):
            logger.info("[mcp] x402 settled for tool=%s", name)
    except Exception as e:
        logger.warning("[mcp] x402 settle failed for tool=%s: %s", name, e)


# ── JSON-RPC helpers ─────────────────────────────────────────────────────────
def _rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _coerce_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Coerce MCP tool arguments to the types provider.py expects.

    MCP clients may send numbers as strings (e.g. from a form field). provider
    functions read params via .get() and sometimes check isinstance(int), so
    we coerce numeric strings to int/float using the same logic as
    x402_server._coerce_params.
    """
    if not isinstance(args, dict):
        return {}
    return _coerce_params(args)


# ── JSON-RPC method handlers ─────────────────────────────────────────────────
def _rpc_initialize(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP initialize — handshake. Returns server capabilities + protocol info."""
    return _rpc_result(req_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": True},
            "resources": {"listChanged": False},
            "prompts": {"listChanged": False},
            "logging": {},
        },
        "serverInfo": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "agent": {
                "name": AGENT_NAME,
                "id": AGENT_ID,
            },
            "payment": {
                "x402": True,
                "network": NETWORK,
                "payTo": PAY_TO,
                "facilitator": FACILITATOR,
            },
        },
    })


def _rpc_tools_list(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP tools/list — return the full tool catalog. Free (discovery)."""
    return _rpc_result(req_id, {"tools": _all_tools()})


def _rpc_tools_call(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP tools/call — invoke a tool. Gated by x402 payment.

    params shape:
        {"name": "<tool name>", "arguments": {<param>: <value>, ...}}
    """
    tool_name = params.get("name")
    if not tool_name or not isinstance(tool_name, str):
        return _rpc_error(req_id, code=-32602, message="missing or invalid 'name' in tools/call params")

    fn = PROVIDER_ENDPOINTS.get(tool_name)
    if fn is None:
        return _rpc_error(
            req_id,
            code=-32601,
            message=f"tool not found: {tool_name}",
            data={"available": sorted(PROVIDER_ENDPOINTS.keys())},
        )

    # Enforce payment. If denied, the error payload carries x402 requirements.
    allowed, err = _check_tool_payment(tool_name)
    if not allowed:
        return err

    raw_args = params.get("arguments", {}) or {}
    args = _coerce_tool_args(raw_args)

    try:
        result = fn(args)
    except Exception as e:
        logger.exception("[mcp] tool %s raised", tool_name)
        return _rpc_error(req_id, code=-32000, message=f"tool execution failed: {e}", data={"tool": tool_name})

    # Settle payment now that the tool succeeded.
    _settle_after_call(tool_name)

    # MCP tools/call result shape: {content: [{type, text}], isError}
    # We serialize the provider's dict result as JSON text content. This is
    # the canonical MCP way to return structured data from a tool.
    return _rpc_result(req_id, {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, default=str, ensure_ascii=False),
            }
        ],
        "isError": isinstance(result, dict) and "error" in result,
    })


def _rpc_resources_list(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP resources/list — we don't expose MCP resources, return empty."""
    return _rpc_result(req_id, {"resources": []})


def _rpc_prompts_list(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP prompts/list — we don't expose MCP prompts, return empty."""
    return _rpc_result(req_id, {"prompts": []})


def _rpc_ping(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    """MCP ping — keepalive. Returns an empty result."""
    return _rpc_result(req_id, {})


# Method dispatch table. Each handler takes (params, id) and returns a
# JSON-RPC response dict. Methods not listed return -32601 method not found.
_RPC_METHODS: dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]] = {
    "initialize": _rpc_initialize,
    "ping": _rpc_ping,
    "tools/list": _rpc_tools_list,
    "tools/call": _rpc_tools_call,
    "resources/list": _rpc_resources_list,
    "prompts/list": _rpc_prompts_list,
}


def _handle_rpc(msg: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single JSON-RPC 2.0 request to its handler."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, code=-32600, message="invalid request: not JSON-RPC 2.0")
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(req_id, code=-32602, message="params must be an object")
    handler = _RPC_METHODS.get(method)
    if handler is None:
        return _rpc_error(req_id, code=-32601, message=f"method not found: {method}")
    try:
        return handler(params, req_id)
    except Exception as e:
        logger.exception("[mcp] handler %s raised", method)
        return _rpc_error(req_id, code=-32603, message=f"internal error: {e}")


# ── SSE notification infrastructure ──────────────────────────────────────────
# MCP uses SSE for server-initiated notifications (e.g. tools/list_changed
# after a redeploy adds an endpoint). We keep a registry of per-client
# queues; a background sweeper doesn't run by default, but a deployment can
# call mcp_notify("notifications/tools/list_changed") to fan out a
# notification to every connected SSE client.
_SSE_CLIENTS: dict[str, "queue.Queue[dict[str, Any]]"] = {}
_SSE_LOCK = threading.Lock()


def _sse_register(client_id: str) -> "queue.Queue[dict[str, Any]]":
    q: "queue.Queue[dict[str, Any]]" = queue.Queue()
    with _SSE_LOCK:
        _SSE_CLIENTS[client_id] = q
    return q


def _sse_unregister(client_id: str) -> None:
    with _SSE_LOCK:
        _SSE_CLIENTS.pop(client_id, None)


def mcp_notify(method: str, params: dict[str, Any] | None = None) -> int:
    """Fan out an MCP notification to every connected SSE client.

    Returns the number of clients notified. Safe to call from any thread;
    called from the request handler thread after a tools/list_changed event
    (e.g. a hot-reload of provider.py that changed ENDPOINTS).
    """
    msg = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    delivered = 0
    with _SSE_LOCK:
        clients = list(_SSE_CLIENTS.values())
    for q in clients:
        try:
            q.put_nowait(msg)
            delivered += 1
        except queue.Full:
            logger.warning("[mcp] SSE client queue full; dropping notification")
    return delivered


# ── Flask Blueprint ──────────────────────────────────────────────────────────
mcp_bp = Blueprint("mcp_server", __name__)


@mcp_bp.route("/.well-known/mcp.json", methods=["GET"])
def mcp_manifest():
    """Serve the MCP server manifest.

    This is a static discovery document (analogous to /.well-known/agent.json
    for A2A and /.well-known/x402 for x402). It lists every tool, the
    transports the server speaks, and the payment configuration. No payment
    required — it's discovery.
    """
    base_url = request.host_url.rstrip("/")
    usdc_cfg = USDC_ASSETS.get(NETWORK, USDC_ASSETS.get("base-sepolia", {}))
    manifest = {
        "protocol": "mcp",
        "protocolVersion": PROTOCOL_VERSION,
        "server": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "agent": {"name": AGENT_NAME, "id": AGENT_ID},
        },
        "transports": {
            "http": {
                "endpoint": f"{base_url}/mcp",
                "method": "POST",
                "contentType": "application/json",
                "spec": "https://www.jsonrpc.org/specification",
            },
            "sse": {
                "endpoint": f"{base_url}/mcp/sse",
                "method": "GET",
                "contentType": "text/event-stream",
            },
        },
        "payment": {
            "x402": {
                "version": X402_VERSION,
                "supported": True,
                "appliesTo": "tools/call",
                "network": NETWORK,
                "asset": usdc_cfg.get("asset"),
                "assetSymbol": "USDC",
                "payTo": PAY_TO,
                "facilitator": FACILITATOR,
                "bypass": [
                    "X-API-Key", "X-Owner-Key", "Authorization: Bearer <key>",
                ],
            },
        },
        "tools": _all_tools(),
        "toolsCount": len(PROVIDER_ENDPOINTS),
    }
    resp = jsonify(manifest)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@mcp_bp.route("/mcp", methods=["POST"])
def mcp_jsonrpc():
    """JSON-RPC 2.0 endpoint. Handles initialize, ping, tools/list, tools/call.

    Accepts either a single request object or a batch array (per JSON-RPC 2.0
    spec §6). Each request is dispatched independently; the response is a
    single object or an array of objects matching the input shape.
    """
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify(_rpc_error(None, code=-32700, message="parse error")), 400

    # Batch request.
    if isinstance(payload, list):
        if not payload:
            return jsonify(_rpc_error(None, code=-32600, message="invalid request: empty batch")), 400
        responses = []
        for msg in payload:
            # JSON-RPC notifications (no id) get no response, even in batch.
            if isinstance(msg, dict) and "id" not in msg:
                # Still dispatch (e.g. a notification ping), but drop the response.
                _handle_rpc(msg)
                continue
            responses.append(_handle_rpc(msg))
        if not responses:
            # All notifications — return 204 No Content.
            return ("", 204)
        return jsonify(responses)

    # Single request.
    if not isinstance(payload, dict):
        return jsonify(_rpc_error(None, code=-32600, message="invalid request")), 400

    # JSON-RPC notification (no id) — process but return 204.
    if "id" not in payload:
        _handle_rpc(payload)
        return ("", 204)

    return jsonify(_handle_rpc(payload))


@mcp_bp.route("/mcp/sse", methods=["GET"])
def mcp_sse():
    """SSE stream for MCP server-initiated notifications.

    A client opens this stream and keeps it alive; the server pushes
    JSON-RPC notification objects (e.g. `notifications/tools/list_changed`)
    as `data:` events whenever mcp_notify() is called. A heartbeat comment
    is sent every 15s to keep proxies from closing the connection.
    """
    client_id = str(uuid.uuid4())
    q = _sse_register(client_id)

    @stream_with_context
    def generate():
        # Per SSE/MCP convention, the first event is an `endpoint` notice
        # telling the client where to POST JSON-RPC requests.
        endpoint_url = request.host_url.rstrip("/") + "/mcp"
        yield f"event: endpoint\ndata: {json.dumps(endpoint_url)}\n\n"
        # Quick welcome notification so the client knows the stream is live.
        yield (
            "data: "
            + json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {"server": SERVER_NAME, "protocolVersion": PROTOCOL_VERSION},
            })
            + "\n\n"
        )
        last_heartbeat = time.monotonic()
        try:
            while True:
                try:
                    msg = q.get(timeout=5)
                    yield "data: " + json.dumps(msg) + "\n\n"
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_heartbeat >= 15:
                        # SSE comment — keeps the connection alive without
                        # emitting a spurious client-side event.
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
        except GeneratorExit:
            pass
        finally:
            _sse_unregister(client_id)

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # disable nginx buffering
    resp.headers["Connection"] = "keep-alive"
    return resp


# CORS preflight for the JSON-RPC endpoint (MCP clients may be cross-origin).
@mcp_bp.route("/mcp", methods=["OPTIONS"])
def mcp_options():
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-PAYMENT, X-API-Key, X-Owner-Key, Authorization"
    )
    return resp


# ── CLI entrypoint for local debugging ───────────────────────────────────────
if __name__ == "__main__":
    # Print the manifest so you can eyeball it without standing up Flask.
    print(json.dumps({
        "protocol": "mcp",
        "protocolVersion": PROTOCOL_VERSION,
        "server": SERVER_NAME,
        "toolsCount": len(PROVIDER_ENDPOINTS),
        "tools": _all_tools(),
    }, indent=2))
