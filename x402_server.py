"""
x402_server.py — x402-compliant (Base/USDC) paid HTTP layer for scriptmasterlabs'
18 data endpoints.

provider.py sells the same 18 endpoints, but only through the Virtuals ACP
marketplace: a headless background worker that listens for on-chain job events
via `acp events listen` and has no public HTTP surface at all. ACP and x402 are
different protocols with different discovery surfaces — an x402 indexer (e.g.
x402scan) has nothing to crawl on the ACP side, which is why those 18
endpoints never showed up there. This file is the fix: it exposes the exact
same data functions over real HTTP with the standard x402 402→pay→settle flow,
so it's actually discoverable and payable by x402/Base agents.

Runs as its own Render *web* service, separate from provider.py's *worker*
service — same repo, same underlying data functions, two independent runtimes.

Every route below requires payment. There is no free tier.
"""
import os
from flask import Flask, jsonify, request

from provider import ENDPOINTS as PROVIDER_ENDPOINTS
from x402_flask import x402_guard, register_x402_discovery

app = Flask(__name__)
register_x402_discovery(app)

# USD price per call — mirrors the pricing already published in README.md for
# the ACP marketplace offerings. Kept here (not added to provider.py) so the
# ACP-only file stays untouched.
_PRICES_USD = {
    "perp_funding_aggregator":        "0.50",
    "market_regime_indicator":        "0.50",
    "defi_yield_rates":               "0.30",
    "defi_tvl_ranking":               "0.30",
    "crypto_market_overview":         "0.20",
    "crypto_price_lookup":            "0.15",
    "stablecoin_flow_tracker":        "0.25",
    "federal_contract_opportunities": "0.50",
    "federal_award_history":          "0.35",
    "sdvosb_setaside_feed":           "0.75",
    "sam_entity_verification":        "0.40",
    "federal_spending_by_agency":     "0.30",
    "excluded_parties_check":         "0.25",
    "crypto_onchain_analytics":       "0.40",
    "crypto_sentiment_scanner":       "0.35",
    "dex_volume_ranking":             "0.25",
    "token_security_audit":           "0.30",
    "whale_wallet_tracker":           "0.40",
}

assert set(_PRICES_USD) == set(PROVIDER_ENDPOINTS), \
    "x402_server's price map has drifted from provider.py's ENDPOINTS registry"


def _coerce_params(raw: dict) -> dict:
    """Query-string values are always strings; provider.py's functions expect
    JSON-typed params (e.g. api_perp_funding_aggregator checks isinstance(top_n,
    int)) since their only caller so far has been ACP's JSON job payloads."""
    coerced = {}
    for k, v in raw.items():
        if v.isdigit():
            coerced[k] = int(v)
        else:
            try:
                coerced[k] = float(v)
            except ValueError:
                coerced[k] = v
    return coerced


def _make_view(name: str, fn, price_usd: str):
    @x402_guard(price_usd, f"scriptmasterlabs — {name.replace('_', ' ')}")
    def _view():
        return jsonify(fn(_coerce_params(request.args.to_dict())))
    _view.__name__ = f"x402_{name}"
    return _view


for _name, _fn in PROVIDER_ENDPOINTS.items():
    app.add_url_rule(
        f"/x402/{_name.replace('_', '-')}",
        endpoint=f"x402_{_name}",
        view_func=_make_view(_name, _fn, _PRICES_USD[_name]),
        methods=["GET"],
    )


@app.route("/")
@app.route("/api/status")
def status():
    return jsonify({
        "service": "acp-provider x402 HTTP layer",
        "agent": "scriptmasterlabs",
        "endpoints": sorted(f"/x402/{n.replace('_', '-')}" for n in PROVIDER_ENDPOINTS),
        "free_tier": False,
        "note": "Every endpoint above requires x402 payment (USDC on Base). See /.well-known/x402 for rails and pricing.",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
