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
MAX_TIMEOUT  = int(os.environ.get("X402_MAX_TIMEOUT", "120"))


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


def _402(requirements: dict, reason: str = ""):
    accepts = [requirements]
    rlusd = _rlusd_requirements(
        price_rlusd=str(float(requirements["maxAmountRequired"]) / 1_000_000),
        description=requirements["description"],
        resource=requirements["resource"],
    )
    if rlusd is not None:
        accepts.append(rlusd)
    body = {
        "x402Version": X402_VERSION,
        "error": reason,
        # v2 top-level `resource` is an OBJECT, not the plain string each
        # accept entry still carries for backward compat. Missing this was
        # confirmed (via the sibling mcp-x402 deployment's own debugging
        # history) to make x402scan/Bazaar discovery reject the response
        # outright rather than just flag it as outdated.
        "resource": {
            "url": requirements["resource"],
            "description": requirements["description"],
            "mimeType": requirements["mimeType"],
        },
        "accepts": accepts,
    }
    resp = make_response(jsonify(body), 402)
    resp.headers["Content-Type"] = "application/json"
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


def x402_guard(price_usdc: str, description: str, discoverable: bool = True):
    def decorator(fn):
        if discoverable:
            DISCOVERY.append({"price_usdc": price_usdc, "description": description, "fn": fn.__name__})

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
                            return _402(reqs, f"AP2 mandate invalid: {verdict['reason']}")
                    elif ap2_mode == "required":
                        return _402(reqs, "AP2 mandate required: send X-AP2-MANDATE header (base64 VC bundle)")
                except ImportError:
                    pass  # ap2 module unavailable — fall through to pure x402

            header = request.headers.get("X-PAYMENT")
            if not header:
                return _402(reqs, "payment required")

            try:
                payment_payload = json.loads(base64.b64decode(header))
            except Exception:
                return _402(reqs, "malformed X-PAYMENT header")

            verify = _facilitator("/verify", payment_payload, reqs)
            if not verify.get("isValid", False):
                return _402(reqs, f"invalid payment: {verify.get('invalidReason', 'unknown')}")

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


def register_x402_discovery(app):
    @app.route("/.well-known/x402")
    def _x402_discovery():
        cfg = USDC.get(NETWORK, USDC["base-sepolia"])
        return jsonify({
            "x402Version": X402_VERSION,
            "operator": "ScriptMasterLabs",
            "discoverable": True,
            "ap2": {
                "supported": True,
                "mode": os.environ.get("AP2_MODE", "optional"),
                "mandate_header": "X-AP2-MANDATE",
                "spec": "https://ap2-protocol.org/specification/",
                "note": "AP2 Intent/Cart/Payment mandates (W3C VCs) verified before honoring agent payments.",
            },
            "rails": [
                {
                    "name": "Base / USDC (x402 standard)",
                    "network": NETWORK,
                    "asset": cfg["asset"],
                    "assetSymbol": "USDC",
                    "payTo": PAY_TO,
                    "facilitator": FACILITATOR,
                    "scheme": "exact",
                },
                {
                    "name": "XRPL / RLUSD (402Proof invoice flow)",
                    "network": "xrpl",
                    "asset": "RLUSD",
                    "assetIssuer": RLUSD_ISSUER,
                    "invoiceEndpoint": f"{PROOF402_BASE}/v1/invoice",
                    "verifyEndpoint":  f"{PROOF402_BASE}/v1/verify",
                    "scheme": "xrpl-invoice",
                },
            ],
            "resources": [
                {"path": d["fn"],
                 "price": {"amount": d["price_usdc"], "assets": ["USDC", "RLUSD"]},
                 "description": d["description"]}
                for d in DISCOVERY
            ],
        })
    return app
