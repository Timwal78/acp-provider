"""
x402_flask.py — Protocol-compliant x402 paywall for the SqueezeOS Flask API.
Real x402 wire protocol (HTTP 402 -> accepts -> X-PAYMENT -> facilitator /verify+/settle)
on USDC over Base. Makes endpoints payable by any x402 agent and discoverable in the
x402 Bazaar when routed through the CDP facilitator.

Dual-rail 402 body: every payment-required response advertises BOTH rails so that
- Standard x402 / Base / USDC agents pick the EVM entry and pay via facilitator.
- RLUSD / XRPL agents pick the XRPL entry and pay via the 402Proof invoice flow
  (POST /v1/invoice → pay on XRPL → POST /v1/verify → retry with X-Payment-Token).
"""

import os
import time
import json
import base64
import secrets
import logging
import requests
from functools import wraps
from flask import request, jsonify, make_response

logger = logging.getLogger("x402")

X402_VERSION = 2

# ── CDP facilitator auth (Coinbase Cloud API JWT — same scheme used across
# CDP/Advanced Trade APIs) ──────────────────────────────────────────────────
# CDP_API_KEY_ID / CDP_API_KEY_SECRET are set directly in Render, never in code.
# CDP_API_KEY_SECRET is the base64 form of a 64-byte Ed25519 secret key
# (32-byte seed + 32-byte public key) as issued by the CDP portal — current
# CDP Secret API Keys are Ed25519, not the older PEM/EC Cloud API Key format.
CDP_API_KEY_ID     = os.environ.get("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET", "")
_CDP_CONFIGURED    = bool(CDP_API_KEY_ID and CDP_API_KEY_SECRET)

NETWORK      = os.environ.get("X402_NETWORK", "base-sepolia").strip().lower()
# No hardcoded fallback wallet: this file was vendored from SqueezeOS, whose
# default here is SqueezeOS's OWN receiving address. Defaulting to it in this
# repo would silently route real USDC payments meant for acp-provider into a
# different product's wallet. Must be set explicitly per deployment.
PAY_TO       = os.environ.get("X402_PAY_TO", "")
# Defaults to the CDP-hosted mainnet facilitator once CDP creds are present,
# otherwise the public signup-free testnet facilitator. Explicit
# X402_FACILITATOR always wins over both.
FACILITATOR  = os.environ.get(
    "X402_FACILITATOR",
    "https://api.cdp.coinbase.com/platform/v2/x402" if _CDP_CONFIGURED else "https://x402.org/facilitator",
).rstrip("/")
MAX_TIMEOUT  = int(os.environ.get("X402_MAX_TIMEOUT", "300"))


def _cdp_ed25519_private_key():
    """Decode CDP_API_KEY_SECRET (64 bytes: 32-byte seed + 32-byte pubkey) into
    an Ed25519 private key, verifying the pubkey half actually derives from the
    seed half — the same corruption check CDP's own SDK performs, so a bad key
    fails loudly here instead of silently at request time."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    raw = base64.b64decode(CDP_API_KEY_SECRET)
    if len(raw) != 64:
        raise ValueError(f"CDP_API_KEY_SECRET must decode to 64 bytes, got {len(raw)}")
    seed, expected_pub = raw[:32], raw[32:]
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    derived_pub = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if derived_pub != expected_pub:
        raise ValueError("CDP_API_KEY_SECRET is corrupted — public-key half does not derive from the seed half")
    return private_key


def _cdp_auth_headers(method: str, host: str, path: str) -> dict:
    """Build the Authorization header for a CDP-authenticated facilitator call.
    Returns {} if CDP creds aren't configured (falls back to unauthenticated,
    which only works against the public testnet facilitator)."""
    if not _CDP_CONFIGURED:
        return {}
    import jwt as _pyjwt

    private_key = _cdp_ed25519_private_key()
    now = int(time.time())
    payload = {
        "sub": CDP_API_KEY_ID,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {host}{path}",
    }
    token = _pyjwt.encode(
        payload,
        private_key,
        algorithm="EdDSA",
        headers={"kid": CDP_API_KEY_ID, "nonce": secrets.token_hex(16)},
    )
    return {"Authorization": f"Bearer {token}"}

# ── RLUSD on XRPL rail (proprietary 402Proof flow) ──
RLUSD_ISSUER  = os.environ.get("RLUSD_ISSUER",  "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De")
PROOF402_BASE = os.environ.get("PROOF402_SERVER_URL", "https://four02proof.onrender.com").rstrip("/")

USDC = {
    "base":         {"asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                     "extra": {"name": "USD Coin", "version": "2"}},
    "base-sepolia": {"asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                     "extra": {"name": "USDC", "version": "2"}},
}

DISCOVERY = []


def _usdc_atomic(price_usdc: str) -> str:
    return str(int(round(float(price_usdc) * 1_000_000)))


def _payment_requirements(price_usdc: str, description: str, resource: str) -> dict:
    cfg = USDC.get(NETWORK, USDC["base-sepolia"])
    units = _usdc_atomic(price_usdc)
    return {
        "scheme": "exact",
        "network": NETWORK,
        # x402 v2 wants both: `amount` is the field the validator checks;
        # `maxAmountRequired` is kept because our own facilitator chain
        # settles off it. Confirmed against a live sibling deployment
        # (SML_Portfolio/mcp-x402) whose engineering notes record this
        # exact validator requirement — not a guess.
        "amount": units,
        "maxAmountRequired": units,
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": MAX_TIMEOUT,
        "asset": cfg["asset"],
        "extra": cfg["extra"],
    }


def _rlusd_requirements(price_rlusd: str, description: str, resource: str) -> "dict | None":
    """
    XRPL/RLUSD entry for the 402 `accepts` array.

    Not native x402 (XRPL has no x402 facilitator), so agents that match this
    entry use the 402Proof invoice/verify flow declared under `extra.flow`.
    Endpoint UUID is looked up by path so the agent can POST it straight to
    /v1/invoice without an extra discovery round-trip.

    SML fix: previously fell back to a hardcoded wallet
    (rUJhaK2ibfTFVdAn8m9jMCcJQ1xo6FmNPZ) nobody on the team recognized or
    held the key to when XRPL_PAY_TO wasn't set — a real risk, since a
    naive x402 client can pay the top-level `payTo` field directly without
    following the documented invoice/verify flow. Returns None instead so
    the caller omits this rail entirely rather than ever advertising an
    unconfigured/unrecognized wallet as a place to send real money.
    """
    xrpl_pay_to = os.environ.get("XRPL_PAY_TO", "")
    if not xrpl_pay_to:
        return None

    try:
        from proof402_integration import ENDPOINTS as _RLUSD_ENDPOINTS
    except Exception:
        _RLUSD_ENDPOINTS = {}

    from urllib.parse import urlparse
    path = urlparse(resource).path or ""
    endpoint_id = _RLUSD_ENDPOINTS.get(path, "")

    return {
        "scheme": "xrpl-invoice",
        "network": "xrpl",
        "amount": str(price_rlusd),
        "maxAmountRequired": str(price_rlusd),
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payTo": xrpl_pay_to,
        "maxTimeoutSeconds": MAX_TIMEOUT,
        "asset": "RLUSD",
        "extra": {
            "name": "Ripple USD",
            "issuer": RLUSD_ISSUER,
            "endpointId": endpoint_id,
            "invoiceEndpoint": f"{PROOF402_BASE}/v1/invoice",
            "verifyEndpoint":  f"{PROOF402_BASE}/v1/verify",
            "tokenHeader":     "X-Payment-Token",
            "walletHeader":    "X-Agent-Wallet",
            "flow": [
                f"1. POST {PROOF402_BASE}/v1/invoice {{\"endpoint_id\":\"{endpoint_id}\"}} → {{pay_to, amount, memo_hex}}",
                "2. Send RLUSD on XRPL to pay_to with memo_hex as MemoData",
                f"3. POST {PROOF402_BASE}/v1/verify {{invoice_id, tx_hash, agent_wallet}} → access_token (1h TTL)",
                f"4. Retry {path} with X-Payment-Token: <access_token> and X-Agent-Wallet: <rWALLET>",
            ],
        },
    }


def _bazaar_extensions(method: str = "GET", query_params: dict | None = None) -> dict:
    """x402scan + Agentic.Market Bazaar extension block.

    x402scan reads extensions.bazaar.schema.properties.input...
    Agentic.Market reads extensions.bazaar.info (DiscoveryInfo shape).
    Both are required for full registration — body-only 402s are rejected
    as "No valid x402 response found" even when status is 402.
    """
    qp = query_params if isinstance(query_params, dict) else {}
    # info wants flat example values, not JSON-schema descriptors
    flat = {}
    schema_props = {}
    for k, v in qp.items():
        if isinstance(v, dict) and "type" in v:
            schema_props[k] = v
            if "example" in v:
                flat[k] = v["example"]
            elif "default" in v:
                flat[k] = v["default"]
            elif v.get("type") in ("integer", "number"):
                flat[k] = 0
            elif v.get("type") == "boolean":
                flat[k] = False
            else:
                flat[k] = ""
        else:
            flat[k] = v if v is not None else ""
            schema_props[k] = {"type": "string", "description": str(k)}
    info = {
        "input": {"type": "http", "method": method, "queryParams": flat},
        "output": {"example": {}},
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "const": "http"},
                    "method": {"type": "string"},
                    "queryParams": {
                        "type": "object",
                        "properties": schema_props,
                    },
                },
                "required": ["type", "method"],
            },
            "output": {"properties": {"example": {}}},
        },
        "required": ["input"],
    }
    return {
        "bazaar": {
            "discoverable": True,
            "info": info,
            "schema": schema,
        }
    }


def _402(requirements: dict, reason: str = "payment_required", query_params: dict | None = None):
    """Emit a scanner-valid x402 v2 payment challenge.

    Must match mcp-x402 / x402scan expectations:
      - body.x402Version, body.error='payment_required', body.resource object,
        body.accepts[], body.extensions.bazaar
      - headers PAYMENT-REQUIRED + X-PAYMENT-REQUIRED = base64(JSON body)
    """
    # USDC/base accept only for scanner validity. RLUSD is a proprietary rail
    # and is NOT a valid x402 accept entry for x402scan — attach only as
    # optional second accept if configured, but keep primary exact/base clean.
    accepts = [{
        "scheme": requirements.get("scheme", "exact"),
        "network": requirements.get("network", NETWORK),
        "amount": requirements.get("amount") or requirements.get("maxAmountRequired"),
        "maxAmountRequired": requirements.get("maxAmountRequired"),
        "asset": requirements.get("asset"),
        "payTo": requirements.get("payTo"),
        "maxTimeoutSeconds": requirements.get("maxTimeoutSeconds", MAX_TIMEOUT),
        "resource": requirements.get("resource"),
        "description": requirements.get("description"),
        "mimeType": requirements.get("mimeType", "application/json"),
        "extra": requirements.get("extra") or {"name": "USD Coin", "version": "2"},
    }]
    # Do NOT append xrpl-invoice into accepts for the challenge that scanners
    # validate — unknown schemes make the whole 402 "invalid". RLUSD stays on
    # /.well-known/x402 rails metadata only.

    err = (reason or "payment_required").strip()
    if err in ("payment required", "Payment Required", ""):
        err = "payment_required"

    body = {
        "x402Version": int(X402_VERSION) if not isinstance(X402_VERSION, int) else X402_VERSION,
        "error": err,
        "resource": {
            "url": requirements["resource"],
            "description": requirements["description"],
            "mimeType": requirements.get("mimeType", "application/json"),
        },
        "accepts": accepts,
        "extensions": _bazaar_extensions("GET", query_params),
    }
    header402 = base64.b64encode(
        json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    resp = make_response(jsonify(body), 402)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["PAYMENT-REQUIRED"] = header402
    resp.headers["X-PAYMENT-REQUIRED"] = header402
    resp.headers["Access-Control-Expose-Headers"] = "PAYMENT-REQUIRED, X-PAYMENT-REQUIRED, X-PAYMENT-RESPONSE"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _facilitator(path: str, payment_payload: dict, requirements: dict) -> dict:
    host = FACILITATOR.split("://", 1)[-1].split("/", 1)[0]
    try:
        headers = _cdp_auth_headers("POST", host, path)
    except Exception as e:
        logger.error("[x402] CDP auth header build failed: %s", e)
        return {"isValid": False, "success": False, "invalidReason": f"cdp_auth_error: {e}"}

    body = {"x402Version": X402_VERSION,
            "paymentPayload": payment_payload,
            "paymentRequirements": requirements}

    # Bazaar discovery extension: CDP indexes a route the first time /settle
    # succeeds for it AND the settle call carries this metadata blob. Omitting
    # it means real mainnet payments could clear fine while the route stays
    # invisible to Agent.market forever — a second, quieter way to look "paid
    # up" but never get discovered.
    if path == "/settle":
        body["extensions"] = {
            "bazaar": {
                "discoverable": True,
                "resource": requirements["resource"],
                "description": requirements["description"],
                "outputSchema": {"type": "object", "properties": {}},
            }
        }

    r = requests.post(f"{FACILITATOR}{path}", json=body, headers=headers, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"isValid": False, "success": False, "invalidReason": f"facilitator {r.status_code}: {r.text[:200]}"}


def x402_guard(price_usdc: str, description: str, discoverable: bool = True, path: str | None = None, name: str | None = None, query_params: dict | None = None):
    """path should be the public hyphen route, e.g. /x402/rwa-aggregates.
    Never rely on fn.__name__ — nested views all become _view and break x402scan.
    """
    def decorator(fn):
        route_path = path or f"/{(name or fn.__name__).replace('_', '-')}"
        if not route_path.startswith('/'):
            route_path = '/' + route_path
        if discoverable:
            DISCOVERY.append({
                "price_usdc": str(price_usdc),
                "description": description,
                "path": route_path,
                "name": name or fn.__name__,
                "fn": fn.__name__,
                "query_params": query_params or {},
            })

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not PAY_TO:
                return jsonify({
                    "error": "ERR_PAYMENT_NOT_CONFIGURED",
                    "message": "X402_PAY_TO is not set on this deployment. Refusing to advertise a payment address.",
                }), 503

            # Force https for public discovery (Render terminates TLS at proxy;
            # request.base_url is often http:// which breaks x402scan/Bazaar).
            proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
            if proto not in ("http", "https"):
                proto = "https"
            host = request.headers.get("X-Forwarded-Host") or request.host
            resource = f"{proto}://{host}{request.path}"
            if proto == "http" and "onrender.com" in host:
                resource = f"https://{host}{request.path}"
            reqs = _payment_requirements(price_usdc, description, resource)

            # ── Operator/agent key bypass ──
            # Mirrors proof402_integration.require_payment's bypass exactly:
            # a request carrying a valid OPERATOR_API_KEY / OWNER_API_KEY / one
            # of AGENT_API_KEYS skips on-chain x402 settlement. Needed for
            # agents (e.g. LEVIATHAN) that already collected payment upstream
            # via ACP and are calling this route as an authorized backend, not
            # as a paying end-user — this decorator previously had no such
            # bypass, so every ACP-resold job routed through it 402'd even
            # after the buyer had already paid LEVIATHAN.
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
                return fn(*args, **kwargs)

            # ── AP2 mandate gate (Google Agent Payments Protocol) ──
            # Modes via env AP2_MODE: "off" | "optional" (default) | "required"
            ap2_mode = os.environ.get("AP2_MODE", "optional").lower()
            if ap2_mode != "off":
                try:
                    from ap2_mandate import verify_mandate, mandate_from_request
                    mandate = mandate_from_request(request.headers)
                    if mandate is not None:
                        verdict = verify_mandate(mandate, {
                            "resource": resource,
                            "amountAtomicUSDC": int(reqs["maxAmountRequired"]),
                            "payTo": PAY_TO,
                            "trustedIssuers": json.loads(os.environ.get("AP2_TRUSTED_ISSUERS", "{}")),
                        })
                        if not verdict["valid"]:
                            return _402(reqs, f"AP2 mandate invalid: {verdict['reason']}", query_params=query_params)
                    elif ap2_mode == "required":
                        return _402(reqs, "AP2 mandate required: send X-AP2-MANDATE header (base64 VC bundle)", query_params=query_params)
                except ImportError:
                    pass  # ap2 module unavailable — fall through to pure x402

            header = request.headers.get("X-PAYMENT")
            if not header:
                return _402(reqs, "payment_required", query_params=query_params)

            try:
                payment_payload = json.loads(base64.b64decode(header))
            except Exception:
                return _402(reqs, "malformed X-PAYMENT header", query_params=query_params)

            verify = _facilitator("/verify", payment_payload, reqs)
            if not verify.get("isValid", False):
                return _402(reqs, f"invalid payment: {verify.get('invalidReason', 'unknown')}", query_params=query_params)

            result = fn(*args, **kwargs)

            settle = _facilitator("/settle", payment_payload, reqs)
            resp = make_response(result)
            if settle.get("success", False):
                resp.headers["X-PAYMENT-RESPONSE"] = base64.b64encode(
                    json.dumps(settle).encode()).decode()
                # SML fix: the RLUSD rail (proof402_integration.require_payment)
                # has always fired a Discord payment alert on success — this
                # Coinbase/USDC rail never did, so a real settled payment left
                # zero trace anywhere except the on-chain transfer itself. An
                # $8 USDC payment surfaced with no record in Discord or the
                # in-memory analytics funnel (which also resets on every
                # redeploy) before this was caught.
                try:
                    from proof402_integration import _fire_payment_discord
                    payer = (
                        settle.get("payer")
                        or payment_payload.get("payload", {}).get("authorization", {}).get("from", "")
                        or "unknown"
                    )
                    _fire_payment_discord(payer, request.path, 2)
                except Exception:
                    pass
            return resp
        return wrapper
    return decorator


def _public_base_url():
    """Prefer explicit public URL; else rebuild from proxy headers as https."""
    explicit = (os.environ.get("X402_PUBLIC_BASE") or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if explicit:
        return explicit
    try:
        from flask import request
        proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
        host = request.headers.get("X-Forwarded-Host") or request.host
        if "onrender.com" in (host or ""):
            proto = "https"
        return f"{proto}://{host}"
    except Exception:
        return "https://acp-x402-scriptmasterlabs.onrender.com"


def _openapi_discovery_doc():
    """OpenAPI 3.1 + x-payment-info — same shape x402scan indexes on mcp-x402."""
    cfg = USDC.get(NETWORK, USDC["base-sepolia"])
    base = _public_base_url()
    paths = {}
    resources = []
    for d in DISCOVERY:
        path = d.get("path") or ""
        if not path or path in ("_view", "/_view"):
            # skip broken legacy entries if any remain
            continue
        if not path.startswith("/"):
            path = "/" + path
        price = str(d["price_usdc"])
        try:
            units = str(int(round(float(price) * 1_000_000)))
        except Exception:
            units = "0"
        name = d.get("name") or path.strip("/").replace("-", "_")
        desc = d.get("description") or f"scriptmasterlabs — {name}"
        op_id = "".join(p.capitalize() for p in name.replace("-", "_").split("_"))
        params_in = []
        qp = d.get("query_params") or {}
        for pk, pv in qp.items():
            schema = {"type": pv.get("type", "string")} if isinstance(pv, dict) else {"type": "string"}
            if isinstance(pv, dict) and "default" in pv:
                schema["default"] = pv["default"]
            params_in.append({
                "name": pk,
                "in": "query",
                "required": False,
                "schema": schema,
                "description": (pv.get("description") if isinstance(pv, dict) else str(pk)),
                **({"example": pv.get("example")} if isinstance(pv, dict) and "example" in pv else {}),
            })
        paths[path] = {
            "get": {
                "operationId": op_id or name,
                "summary": desc,
                "description": f"{desc}. Pay {price} USDC on Base via x402, then retry with X-PAYMENT.",
                "parameters": params_in,
                "x-payment-info": {
                    "method": "x402",
                    "scheme": "exact",
                    "network": NETWORK,
                    "asset": cfg["asset"],
                    "currency": "USDC",
                    "amount": price,
                    "amountUnits": units,
                    "payTo": PAY_TO,
                    "settlement": "facilitator",
                    "facilitator": FACILITATOR,
                    "paymentHeader": "X-PAYMENT",
                    "protocols": ["x402"],
                    "price": {"mode": "fixed", "currency": "USD", "amount": price},
                },
                "responses": {
                    "200": {"description": "Paid JSON result"},
                    "402": {"description": "Payment required — pay USDC on Base then retry with X-PAYMENT."},
                },
            }
        }
        resources.append({
            "path": path,
            "url": f"{base}{path}",
            "name": name,
            "description": desc,
            "price": {"amount": price, "assets": ["USDC"]},
            "network": NETWORK,
            "payTo": PAY_TO,
            "facilitator": FACILITATOR,
            "scheme": "exact",
        })

    # free discovery aliases
    for free_path, op in [
        ("/.well-known/x402", "openApiDiscovery"),
        ("/x402/openapi.json", "openApiJsonAlias"),
        ("/openapi.json", "openApiJson"),
    ]:
        paths[free_path] = {
            "get": {
                "operationId": op,
                "summary": "OpenAPI/x402 discovery document (free).",
                "security": [],
                "responses": {"200": {"description": "OpenAPI spec."}},
            }
        }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "ScriptMasterLabs — ACP x402 Data API",
            "version": "1.1.0",
            "description": (
                "Pay-per-call crypto, RWA, federal, SEC, and compliance APIs. "
                "Settled in USDC on Base via x402. Hyphen routes only "
                "(e.g. /x402/rwa-aggregates, /x402/gas-tracker)."
            ),
            "contact": {
                "name": "ScriptMasterLabs",
                "url": "https://www.scriptmasterlabs.com",
                "email": "hello@scriptmasterlabs.com",
            },
        },
        "servers": [{"url": base}],
        "x-service-info": {
            "operator": "ScriptMasterLabs",
            "agent": "scriptmasterlabs",
            "discoverable": True,
            "categories": [
                "rwa", "crypto", "defi", "gas", "sec-filings", "federal-contracts",
                "compliance", "market-intelligence", "macro",
            ],
            "payment": {
                "protocol": "x402",
                "rails": [
                    {
                        "id": "base-usdc",
                        "scheme": "exact",
                        "network": NETWORK,
                        "asset": cfg["asset"],
                        "assetSymbol": "USDC",
                        "payTo": PAY_TO,
                        "facilitator": FACILITATOR,
                        "settlement": "facilitator",
                        "paymentHeader": "X-PAYMENT",
                    }
                ],
            },
            "docs": "https://timwal78.github.io/acp-provider/rwa-api.html",
            "ap2": {
                "supported": True,
                "mode": os.environ.get("AP2_MODE", "optional"),
                "mandate_header": "X-AP2-MANDATE",
            },
        },
        # dual format: OpenAPI paths (x402scan/mcp-x402 style) + resources array
        "paths": paths,
        "x402Version": X402_VERSION,
        "discoverable": True,
        "operator": "ScriptMasterLabs",
        "rails": [
            {
                "name": "Base / USDC (x402 standard)",
                "network": NETWORK,
                "asset": cfg["asset"],
                "assetSymbol": "USDC",
                "payTo": PAY_TO,
                "facilitator": FACILITATOR,
                "scheme": "exact",
                "paymentHeader": "X-PAYMENT",
            },
            {
                "name": "XRPL / RLUSD (402Proof invoice flow)",
                "network": "xrpl",
                "asset": "RLUSD",
                "assetIssuer": RLUSD_ISSUER,
                "invoiceEndpoint": f"{PROOF402_BASE}/v1/invoice",
                "verifyEndpoint": f"{PROOF402_BASE}/v1/verify",
                "scheme": "xrpl-invoice",
            },
        ],
        "resources": resources,
    }


def register_x402_discovery(app):
    def _x402_discovery():
        return jsonify(_openapi_discovery_doc())

    app.add_url_rule("/.well-known/x402", endpoint="x402_discovery", view_func=_x402_discovery, methods=["GET"])
    app.add_url_rule("/x402/openapi.json", endpoint="x402_openapi", view_func=_x402_discovery, methods=["GET"])
    app.add_url_rule("/openapi.json", endpoint="openapi_json", view_func=_x402_discovery, methods=["GET"])
    return app
