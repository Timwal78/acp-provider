
def _apply_cors(app):
    """Browser dashboards (scriptmasterlabs.com) must read discovery manifests cross-origin."""
    @app.before_request
    def _cors_before():
        from flask import request, make_response
        if request.method == "OPTIONS":
            r = make_response("", 204)
            r.headers["Access-Control-Allow-Origin"] = "*"
            r.headers["Access-Control-Allow-Methods"] = "GET,HEAD,POST,OPTIONS"
            r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-PAYMENT, X-PAYMENT-TX, PAYMENT-SIGNATURE, Accept"
            return r

    @app.after_request
    def _cors_after_request(resp):
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        resp.headers.setdefault("Access-Control-Allow-Methods", "GET,HEAD,POST,OPTIONS")
        resp.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, X-PAYMENT, X-PAYMENT-TX, PAYMENT-SIGNATURE, Accept",
        )
        resp.headers.setdefault(
            "Access-Control-Expose-Headers",
            "PAYMENT-REQUIRED, X-PAYMENT-REQUIRED, X-PAYMENT-RESPONSE",
        )
        return resp


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
import urllib.request
import urllib.error
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
# Asset map key (base | base-sepolia). Challenge network must be CAIP-2 for x402scan.
_NETWORK_KEY = "base" if NETWORK in ("base", "eip155:8453", "8453") else (
    "base-sepolia" if "sepolia" in NETWORK else NETWORK
)
NETWORK_CAIP = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
}.get(_NETWORK_KEY, NETWORK if NETWORK.startswith("eip155:") else f"eip155:8453")
# No hardcoded fallback wallet: this file was vendored from SqueezeOS, whose
# default here is SqueezeOS's OWN receiving address. Defaulting to it in this
# repo would silently route real USDC payments meant for acp-provider into a
# different product's wallet. Must be set explicitly per deployment.
_PAY_TO_RAW = (os.environ.get("X402_PAY_TO") or "").strip()
_ACP_SAFE_PAY = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
if _PAY_TO_RAW.lower().startswith("0x4e14"):
    import logging as _log
    _log.getLogger("x402").error("REFUSED orphan X402_PAY_TO 0x4e14… — forcing ACP 0x7233…")
    PAY_TO = _ACP_SAFE_PAY
else:
    PAY_TO = _PAY_TO_RAW
# Defaults to the CDP-hosted mainnet facilitator once CDP creds are present,
# otherwise the public signup-free testnet facilitator. Explicit
# X402_FACILITATOR always wins over both.
FACILITATOR  = os.environ.get(
    "X402_FACILITATOR",
    "https://api.cdp.coinbase.com/platform/v2/x402" if _CDP_CONFIGURED else "https://x402.org/facilitator",
).rstrip("/")
MAX_TIMEOUT  = int(os.environ.get("X402_MAX_TIMEOUT", "300"))

def _amb_count_paid(payer: str | None = None) -> None:
    """Best-effort AMB paid_call counter (never raises)."""
    try:
        from amb import record_amb_traffic, _agent_key_from_request
        key = (payer or "").strip() or _agent_key_from_request()
        record_amb_traffic("paid_call", key[:42] if key else "anon")
    except Exception:
        pass



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

# Robinhood Chain / Global Dollar (USDG) — non-CDP discovery + sovereign X-PAYMENT-TX.
# Confirmed live: chainId 4663, USDG 0x5fc5… 6 decimals on robinhoodchain.blockscout.com.
ROBINHOOD_CAIP = "eip155:4663"
SOLANA_CAIP = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAY_TO = os.environ.get("SOLANA_PAYMENT_RECEIVER", "E4d3JwcTjeqTRkkQS4moszcfa4R7G1NMgPSew4KBNFrB").strip()
USDG_ROBINHOOD = os.environ.get(
    "USDG_ROBINHOOD_ASSET",
    "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
)
ROBINHOOD_RPCS = [u for u in [
    (os.environ.get("ROBINHOOD_RPC_URL") or "").strip(),
    "https://rpc.mainnet.chain.robinhood.com",
] if u]

USDC = {
    "base":         {"asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                     "extra": {"name": "USD Coin", "version": "2"}},
    "base-sepolia": {"asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                     "extra": {"name": "USDC", "version": "2"}},
}


# ── Sovereign rail: on-chain Transfer verified via public RPC ──
# Clients pay then retry with header X-PAYMENT-TX=<txHash>.
# No facilitator. Replay-protected in-process (resets on redeploy).
# Tries Base USDC first (CDP-compatible money path), then Robinhood USDG.
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_REDEEMED_TXS: set[str] = set()
_BASE_RPCS = [u for u in [
    (os.environ.get("BASE_RPC_URL") or "").strip(),
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://base.gateway.tenderly.co",
    "https://base.meowrpc.com",
    "https://base.llamarpc.com",
] if u]

def _rpc_call(method: str, params: list, rpcs: list | None = None):
    """JSON-RPC with UA — bare python-urllib gets 403 from several public RPCs."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "scriptmasterlabs-x402-sovereign/1.0 (+https://www.scriptmasterlabs.com)",
    }
    last_err = None
    for rpc in (rpcs or _BASE_RPCS):
        try:
            req = urllib.request.Request(rpc, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            if d.get("error"):
                last_err = d["error"]
                continue
            if d.get("result") is not None:
                return d["result"]
        except Exception as e:
            last_err = str(e)[:120]
            continue
    return None

def _topic_to_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()

def _verify_erc20_tx(tx_hash: str, pay_to: str, min_units: int, asset: str, rpcs: list, miss_error: str, chain_tag: str) -> dict:
    """Return {ok, from?, amount?, chain?, error?} for a confirmed ERC-20 Transfer."""
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return {"ok": False, "error": "invalid_tx_hash_format"}
    h = tx_hash.lower()
    if h in _REDEEMED_TXS:
        return {"ok": False, "error": "payment_already_redeemed"}
    receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash], rpcs=rpcs)
    if not receipt:
        return {"ok": False, "error": "tx_not_found_or_pending_or_rpc_blocked"}
    if receipt.get("status") not in ("0x1", 1, "0x01"):
        return {"ok": False, "error": "tx_reverted"}
    asset_lc = (asset or "").lower()
    pay = (pay_to or PAY_TO).lower()
    for log in receipt.get("logs") or []:
        if (log.get("address") or "").lower() != asset_lc:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        if (topics[0] or "").lower() != _TRANSFER_TOPIC:
            continue
        to_addr = _topic_to_addr(topics[2])
        if to_addr != pay:
            continue
        try:
            value = int(log.get("data") or "0x0", 16)
        except Exception:
            continue
        if value < int(min_units):
            continue
        frm = _topic_to_addr(topics[1]) if topics[1] else ""
        return {"ok": True, "from": frm, "amount": value, "chain": chain_tag, "asset": asset}
    return {"ok": False, "error": miss_error}

def _verify_base_usdc_tx(tx_hash: str, pay_to: str, min_units: int) -> dict:
    """Return {ok, from?, amount?, error?} for a confirmed USDC transfer to pay_to on Base."""
    usdc = USDC.get(_NETWORK_KEY, USDC.get("base", {})).get("asset", "")
    return _verify_erc20_tx(
        tx_hash, pay_to, min_units, usdc, _BASE_RPCS, "no_matching_usdc_transfer", "base",
    )

def _verify_robinhood_usdg_tx(tx_hash: str, pay_to: str, min_units: int) -> dict:
    """Confirmed USDG transfer on Robinhood Chain 4663 (non-CDP rail)."""
    return _verify_erc20_tx(
        tx_hash, pay_to, min_units, USDG_ROBINHOOD, ROBINHOOD_RPCS,
        "no_matching_usdg_transfer", "robinhood",
    )

def _verify_solana_usdc_tx(tx_hash: str, pay_to: str, min_units: int) -> dict:
    """SPL USDC transfer to Solana payTo (owner). tx_hash = base58 signature."""
    sig = (tx_hash or "").strip()
    if not sig or sig.startswith("0x") or len(sig) < 64 or len(sig) > 128:
        return {"ok": False, "error": "invalid_solana_sig_format"}
    if sig.lower() in _REDEEMED_TXS:
        return {"ok": False, "error": "payment_already_redeemed"}
    sol_pay = (pay_to or SOLANA_PAY_TO or "").strip()
    if not sol_pay or sol_pay.startswith("0x"):
        return {"ok": False, "error": "invalid_solana_payTo"}
    rpc = (os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").strip()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    }
    try:
        import urllib.request
        req = urllib.request.Request(
            rpc,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "acp-x402-sol-verify/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"solana_rpc_{e}"}
    if j.get("error"):
        return {"ok": False, "error": f"solana_rpc_{j['error']}"}
    tx = j.get("result")
    if not tx:
        return {"ok": False, "error": "solana_tx_not_found_or_pending"}
    if (tx.get("meta") or {}).get("err"):
        return {"ok": False, "error": "solana_tx_failed"}
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    def rows(bal_list):
        out = []
        for r in bal_list:
            if not r or str(r.get("mint")) != USDC_SOLANA_MINT:
                continue
            try:
                amt = int((r.get("uiTokenAmount") or {}).get("amount") or 0)
            except Exception:
                amt = 0
            out.append({"owner": r.get("owner"), "amount": amt, "idx": r.get("accountIndex")})
        return out

    pre_b, post_b = rows(pre), rows(post)
    for pb in post_b:
        if pb.get("owner") != sol_pay:
            continue
        before = 0
        for a in pre_b:
            if a.get("owner") == sol_pay:
                before = a["amount"]
                break
        delta = pb["amount"] - before
        if delta >= min_units:
            return {
                "ok": True,
                "from": "",
                "amount": delta,
                "asset": USDC_SOLANA_MINT,
                "chain": "solana",
            }
    return {"ok": False, "error": "no_matching_solana_usdc_transfer"}


def _verify_sovereign_tx(tx_hash: str, pay_to: str, min_units: int) -> dict:
    """Base USDC, Robinhood USDG, or Solana SPL USDC (sig in X-PAYMENT-TX)."""
    sig = (tx_hash or "").strip()
    if sig and not sig.startswith("0x"):
        sol_hit = _verify_solana_usdc_tx(sig, SOLANA_PAY_TO, min_units)
        if sol_hit.get("ok"):
            return sol_hit
        # base58-ish — don't fall through to EVM
        if len(sig) >= 64 and not all(c in "0123456789abcdefABCDEF" for c in sig):
            return sol_hit
    base_hit = _verify_base_usdc_tx(tx_hash, pay_to, min_units)
    if base_hit.get("ok"):
        return base_hit
    rh_hit = _verify_robinhood_usdg_tx(tx_hash, pay_to, min_units)
    if rh_hit.get("ok"):
        return rh_hit
    return {
        "ok": False,
        "error": f"sovereign_unverified base={base_hit.get('error')} robinhood={rh_hit.get('error')}",
    }


DISCOVERY = []


def _usdc_atomic(price_usdc: str) -> str:
    return str(int(round(float(price_usdc) * 1_000_000)))


def _payment_requirements(price_usdc: str, description: str, resource: str) -> dict:
    cfg = USDC.get(_NETWORK_KEY, USDC.get(NETWORK, USDC["base-sepolia"]))
    units = _usdc_atomic(price_usdc)
    return {
        "scheme": "exact",
        "network": NETWORK_CAIP,
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
    # Slot 0 = Base USDC (CDP / x402scan primary). Slot 1 = Robinhood USDG
    # (non-CDP discovery + sovereign X-PAYMENT-TX). Never replace Base.
    # RLUSD stays on /.well-known/x402 rails metadata only — not in accepts.
    _amt = requirements.get("amount") or requirements.get("maxAmountRequired")
    # CAIP-2 only — x402scan rejects plain 'base' in any accepts[] slot (2026-07-25).
    accepts = [{
        "scheme": requirements.get("scheme", "exact"),
        "network": NETWORK_CAIP,  # force CAIP-2 for scan; ignore plain base from env
        "amount": _amt,
        "maxAmountRequired": requirements.get("maxAmountRequired") or _amt,
        "asset": requirements.get("asset"),
        "payTo": requirements.get("payTo"),
        "maxTimeoutSeconds": requirements.get("maxTimeoutSeconds", MAX_TIMEOUT),
        "resource": requirements.get("resource"),
        "description": requirements.get("description"),
        "mimeType": requirements.get("mimeType", "application/json"),
        "extra": requirements.get("extra") or {"name": "USD Coin", "version": "2"},
    }, {
        "scheme": "exact",
        "network": ROBINHOOD_CAIP,
        "amount": _amt,
        "maxAmountRequired": requirements.get("maxAmountRequired") or _amt,
        "asset": USDG_ROBINHOOD,
        "payTo": requirements.get("payTo") or PAY_TO,
        "maxTimeoutSeconds": requirements.get("maxTimeoutSeconds", MAX_TIMEOUT),
        "resource": requirements.get("resource"),
        "description": requirements.get("description"),
        "mimeType": requirements.get("mimeType", "application/json"),
        "extra": {
            "name": "Global Dollar",
            "symbol": "USDG",
            "version": "1",
            "decimals": 6,
            "chainId": 4663,
            "settlement": "sovereign-tx",
            "paymentHeader": "X-PAYMENT-TX",
            "rpc": "https://rpc.mainnet.chain.robinhood.com",
            "explorer": "https://robinhoodchain.blockscout.com",
        },
    }, {
        "scheme": "exact",
        "network": SOLANA_CAIP,
        "amount": _amt,
        "maxAmountRequired": requirements.get("maxAmountRequired") or _amt,
        "asset": USDC_SOLANA_MINT,
        "payTo": SOLANA_PAY_TO,
        "maxTimeoutSeconds": requirements.get("maxTimeoutSeconds", MAX_TIMEOUT),
        "resource": requirements.get("resource"),
        "description": requirements.get("description"),
        "mimeType": requirements.get("mimeType", "application/json"),
        "extra": {
            "name": "USD Coin",
            "symbol": "USDC",
            "version": "2",
            "decimals": 6,
            "settlement": "sovereign-tx",
            "paymentHeader": "X-PAYMENT-TX",
            "chain": "solana-mainnet",
            "rpc": os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
            "explorer": "https://solscan.io",
            "note": "Pay SPL USDC on Solana to payTo, retry with X-PAYMENT-TX=<base58 sig>",
        },
    }]

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
    resp.headers["Access-Control-Expose-Headers"] = (
        "PAYMENT-REQUIRED, X-PAYMENT-REQUIRED, X-PAYMENT-RESPONSE, "
        "X-x402-Negotiate, X-x402-Ask, X-x402-Floor, X-x402-Ceiling, X-x402-Batch-Size, X-BEACON-Version"
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # BEACON machine-level bid negotiation (optional headers on request)
    try:
        from beacon import apply_negotiate_headers

        apply_negotiate_headers(resp)
    except Exception:
        pass
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


def _beacon_auto_attest(path: str, ok: bool = True, latency_ms: float = 0.0, agent_id: str = "paid") -> None:
    """Best-effort quality receipt after successful paid execution."""
    try:
        from beacon import attest

        tool = (path or "").rstrip("/").split("/")[-1].replace("-", "_")
        if not tool:
            return
        attest(
            tool=tool,
            ok=ok,
            latency_ms=float(latency_ms or 0),
            schema_valid=True,
            agent_id=agent_id or "paid",
            note="auto_paid_success",
        )
    except Exception:
        pass


def _beacon_record_settlement(
    *,
    path: str,
    amount=None,
    asset: str = "USDC",
    chain: str = "base",
    rail: str = "x402",
    tx: str | None = None,
    payer: str | None = None,
    latency_ms: float | None = None,
) -> None:
    try:
        from beacon import record_settlement

        record_settlement(
            route=path or "",
            amount=amount,
            asset=asset,
            chain=chain,
            rail=rail,
            tx=tx,
            payer=payer,
            latency_ms=latency_ms,
            ok=True,
            source="acp-x402",
        )
    except Exception:
        pass


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

            # ── Operator / marketplace key bypass ──
            # Skip on-chain x402 when caller presents a pre-shared key.
            # Used by: ACP resellers, ZylaLabs marketplace validation (SEND),
            # and operator tools. Keys from env (comma-separated) plus optional
            # baked marketplace key so directory validators get HTTP 200 with
            # Authorization: Bearer <key> instead of a 402 challenge.
            auth_header = request.headers.get("Authorization", "")
            bearer_key = ""
            if auth_header.lower().startswith("bearer "):
                bearer_key = auth_header.split(" ", 1)[1].strip()
            elif "Bearer " in auth_header:
                bearer_key = auth_header.split("Bearer ", 1)[-1].strip()
            # Query fallbacks for importers that only hit the URL bar
            q_key = (
                request.args.get("api_key")
                or request.args.get("apikey")
                or request.args.get("key")
                or ""
            ).strip()
            passed_key = (
                request.headers.get("X-Owner-Key")
                or request.headers.get("X-API-Key")
                or request.headers.get("X-Marketplace-Key")
                or request.headers.get("X-Zyla-Key")
                or bearer_key
                or q_key
            )
            def _split_keys(env_name: str) -> list[str]:
                return [k.strip() for k in os.environ.get(env_name, "").split(",") if k.strip()]
            # Free-pass keys: ONLY explicit env allowlists. No baked defaults.
            # Testers who did not approve must pay x402 — never ship a public bypass.
            agent_keys = _split_keys("AGENT_API_KEYS")
            market_keys = _split_keys("MARKETPLACE_API_KEYS") + _split_keys("ZYLA_API_KEYS")
            valid_keys = (
                [k for k in [os.environ.get("OPERATOR_API_KEY"), os.environ.get("OWNER_API_KEY"), os.environ.get("ZYLA_API_KEY")] if k]
                + agent_keys
                + market_keys
            )
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

            # Rail B (sovereign): X-PAYMENT-TX = on-chain Transfer (Base USDC, then Robinhood USDG)
            tx_hash = (
                request.headers.get("X-PAYMENT-TX")
                or request.headers.get("x-payment-tx")
                or ""
            ).strip()
            if tx_hash:
                min_units = int(reqs.get("amount") or reqs.get("maxAmountRequired") or 0)
                v = _verify_sovereign_tx(tx_hash, reqs.get("payTo") or PAY_TO, min_units)
                if not v.get("ok"):
                    return _402(reqs, f"invalid_sovereign_payment: {v.get('error')}", query_params=query_params)
                result = fn(*args, **kwargs)
                _REDEEMED_TXS.add(tx_hash.lower())
                resp = make_response(result)
                if isinstance(result, dict):
                    # attach paid receipt when JSON
                    pass
                chain = v.get("chain") or "base"
                rail = ("sovereign:solana-usdc" if chain == "solana" else "sovereign:robinhood-usdg" if chain == "robinhood" else "sovereign:base-usdc")
                resp.headers["X-PAYMENT-RESPONSE"] = base64.b64encode(json.dumps({
                    "success": True,
                    "rail": rail,
                    "chain": chain,
                    "tx": tx_hash,
                    "payer": v.get("from") or "unknown",
                    "amount": v.get("amount"),
                    "asset": v.get("asset"),
                }, separators=(",", ":")).encode()).decode()
                _amb_count_paid(v.get("from") or "unknown")
                try:
                    from proof402_integration import _fire_payment_discord
                    _fire_payment_discord(v.get("from") or "unknown", request.path, 2)
                except Exception:
                    pass
                _beacon_auto_attest(request.path, ok=True, agent_id=v.get("from") or "unknown")
                _beacon_record_settlement(
                    path=request.path,
                    amount=v.get("amount") or reqs.get("amount") or reqs.get("maxAmountRequired"),
                    asset=str(v.get("asset") or "USDC"),
                    chain=str(chain or "base"),
                    rail=rail,
                    tx=tx_hash,
                    payer=v.get("from") or "unknown",
                )
                return resp

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
                payer = (
                    settle.get("payer")
                    or payment_payload.get("payload", {}).get("authorization", {}).get("from", "")
                    or "unknown"
                )
                _amb_count_paid(payer)
                try:
                    from proof402_integration import _fire_payment_discord
                    _fire_payment_discord(payer, request.path, 2)
                except Exception:
                    pass
                _beacon_auto_attest(request.path, ok=True, agent_id=payer)
                _beacon_record_settlement(
                    path=request.path,
                    amount=reqs.get("amount") or reqs.get("maxAmountRequired") or settle.get("amount"),
                    asset="USDC",
                    chain="base",
                    rail="facilitator",
                    tx=settle.get("transaction") or settle.get("txHash") or settle.get("tx"),
                    payer=payer,
                )
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
    cfg = USDC.get(_NETWORK_KEY, USDC.get(NETWORK, USDC["base-sepolia"]))
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
            "resource": f"{base}{path}",
            "name": name,
            "description": desc,
            "method": "GET",
            "price": {"amount": price, "assets": ["USDC", "USDG", "USDC-SOL"]},
            "price_usd": float(price) if price.replace(".", "", 1).isdigit() else 0.001,
            "asset": "USDC",
            "assets": ["USDC", "USDG", "USDC-SOL"],
            "network": NETWORK,
            "networks": list(dict.fromkeys([
                NETWORK,
                "eip155:8453" if NETWORK in ("base", "base-mainnet") else NETWORK,
                ROBINHOOD_CAIP,
                "eip155:4663",
                SOLANA_CAIP,
            ])),
            "payTo": PAY_TO,
            "payToByNetwork": {
                NETWORK: PAY_TO,
                "eip155:8453": PAY_TO,
                ROBINHOOD_CAIP: PAY_TO,
                "base": PAY_TO,
                "robinhood": PAY_TO,
                SOLANA_CAIP: SOLANA_PAY_TO,
                "solana": SOLANA_PAY_TO,
            },
            "facilitator": FACILITATOR,
            "scheme": "exact",
            "paid": True,
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
                    },
                    {
                        "id": "robinhood-usdg",
                        "scheme": "exact",
                        "network": ROBINHOOD_CAIP,
                        "asset": USDG_ROBINHOOD,
                        "assetSymbol": "USDG",
                        "payTo": PAY_TO,
                        "settlement": "sovereign-tx",
                        "paymentHeader": "X-PAYMENT-TX",
                        "chainId": 4663,
                        "rpc": "https://rpc.mainnet.chain.robinhood.com",
                        "note": "non-CDP — pay on Robinhood Chain then retry with X-PAYMENT-TX",
                    },
                    {
                        "id": "solana-usdc",
                        "scheme": "exact",
                        "network": SOLANA_CAIP,
                        "asset": USDC_SOLANA_MINT,
                        "assetSymbol": "USDC",
                        "payTo": SOLANA_PAY_TO,
                        "settlement": "sovereign-tx",
                        "paymentHeader": "X-PAYMENT-TX",
                        "note": "Pay SPL USDC on Solana then X-PAYMENT-TX=<base58 sig>",
                    },
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
                "name": "Robinhood Chain / USDG (sovereign X-PAYMENT-TX)",
                "network": ROBINHOOD_CAIP,
                "asset": USDG_ROBINHOOD,
                "assetSymbol": "USDG",
                "payTo": PAY_TO,
                "scheme": "exact",
                "settlement": "sovereign-tx",
                "paymentHeader": "X-PAYMENT-TX",
                "chainId": 4663,
                "rpc": "https://rpc.mainnet.chain.robinhood.com",
                "explorer": "https://robinhoodchain.blockscout.com",
            },
            {
                "name": "Solana / USDC (sovereign X-PAYMENT-TX)",
                "network": SOLANA_CAIP,
                "asset": USDC_SOLANA_MINT,
                "assetSymbol": "USDC",
                "payTo": SOLANA_PAY_TO,
                "scheme": "exact",
                "settlement": "sovereign-tx",
                "paymentHeader": "X-PAYMENT-TX",
                "rpc": os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
                "explorer": "https://solscan.io",
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
        "resourceUrls": [r["url"] for r in resources],
        "network": NETWORK,
        "networks": list(dict.fromkeys([
            NETWORK,
            "eip155:8453" if NETWORK in ("base", "base-mainnet") else NETWORK,
            ROBINHOOD_CAIP,
            "eip155:4663",
            SOLANA_CAIP,
        ])),
        "payTo": PAY_TO,
        "payToByNetwork": {
            NETWORK: PAY_TO,
            "eip155:8453": PAY_TO,
            ROBINHOOD_CAIP: PAY_TO,
            "base": PAY_TO,
            "robinhood": PAY_TO,
            SOLANA_CAIP: SOLANA_PAY_TO,
            "solana": SOLANA_PAY_TO,
        },
        "toolCount": len(resources),
        "paidToolCount": len(resources),
        "health": 1,
        "routable": True,
        "origin": base,
        "homepage": "https://www.scriptmasterlabs.com",
        "beacon": {
            "version": "1.0.0",
            "manifest": f"{base}/.well-known/x402.json",
            "query": f"{base}/beacon/query",
            "negotiate": f"{base}/beacon/negotiate",
            "attest": f"{base}/beacon/attest",
            "status": f"{base}/beacon/status",
            "bid_header": "X-x402-Bid",
            "negotiate_header": "X-x402-Negotiate",
            "docs": "https://www.scriptmasterlabs.com/x402-beacon.html",
            "wedges": [
                f"{base}/x402/gas-tracker",
                f"{base}/x402/crypto-price",
                f"{base}/x402/rwa-aggregates",
            ],
        },
    }


def register_x402_discovery(app):
    _apply_cors(app)

    def _x402_discovery():
        return jsonify(_openapi_discovery_doc())

    app.add_url_rule("/.well-known/x402", endpoint="x402_discovery", view_func=_x402_discovery, methods=["GET"])
    app.add_url_rule("/x402/openapi.json", endpoint="x402_openapi", view_func=_x402_discovery, methods=["GET"])
    app.add_url_rule("/openapi.json", endpoint="openapi_json", view_func=_x402_discovery, methods=["GET"])
    return app
