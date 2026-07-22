"""
x402_server.py — x402-compliant (Base/USDC) paid HTTP layer for scriptmasterlabs'
40 data endpoints.

provider.py sells the same 40 endpoints, but only through the Virtuals ACP
marketplace: a headless background worker that listens for on-chain job events
via `acp events listen` and has no public HTTP surface at all. ACP and x402 are
different protocols with different discovery surfaces — an x402 indexer (e.g.
x402scan) has nothing to crawl on the ACP side. This file is the fix: it
exposes the exact same data functions over real HTTP with the standard x402
402→pay→settle flow, so it's actually discoverable and payable by x402/Base
agents.

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

# USD price per call. x402 HTTP pricing is set independently from the ACP
# marketplace offering priceValue (ACP prices are micro-USD for agent-to-agent
# job escrow; x402 prices are per-HTTP-call for direct payer access). Every
# endpoint in PROVIDER_ENDPOINTS MUST appear here — the assert below enforces
# it so the server fails loudly on drift instead of silently 404'ing routes.
_PRICES_USD = {
    # --- Crypto analytics (real-time market data) ---
    "perp_funding_aggregator":          "0.50",
    "market_regime_indicator":          "0.50",
    "defi_yield_rates":                 "0.30",
    "defi_yield":                       "0.30",  # SEO alias → defi_yield_rates
    "defi_tvl_ranking":                 "0.30",
    "token_security_audit":             "0.30",
    "rugpull_detector":                 "0.25",
    "honeypot_check":                   "0.25",  # SEO alias → rugpull_detector
    "trending_tokens":                  "0.20",
    "smart_money_alerts":               "0.25",
    "new_token_detection":              "0.20",
    "gas_tracker":                      "0.15",
    "stablecoin_flow_tracker":          "0.25",
    "rwa_assets":                      "0.25",
    "rwa_valuation":                   "0.35",
    "rwa_risk":                        "0.25",
    "rwa_aggregates":                  "0.25",
    "rwa_intelligence":                "0.35",
    "wallet_analyzer":                  "0.40",
    "wallet_analysis":                  "0.40",  # SEO alias → wallet_analyzer
    "airdrop_check":                    "0.30",
    "liquidation_risk_check":           "0.35",
    # --- SEC EDGAR filings ---
    "sec_10_k_annual_filing":           "0.25",
    "sec_10_q_quarterly_filing":        "0.25",
    "sec_8_k_real_time_filings":        "0.30",
    "sec_insider_trade_intel":          "0.35",
    "sec_13f_institutional_holdings":   "0.40",
    "sec_13d_13g_activist_filings":     "0.35",
    # --- FDA safety ---
    "fda_warning_letters":              "0.25",
    "fda_drug_recall_alert":            "0.25",
    "fda_adverse_events_report":        "0.30",
    # --- EPA / OSHA enforcement ---
    "epa_environmental_violations":     "0.30",
    "osha_inspection_records":          "0.30",
    # --- FEC / FRED / Congress / Lobbying ---
    "fec_campaign_finance":             "0.25",
    "fred_economic_indicators":         "0.25",
    "congressional_bills_search":       "0.25",
    "lobbying_disclosures":             "0.30",
    # --- AI / Compliance / Macro ---
    "ai_fact_check":                    "0.20",
    "entity_compliance_check":          "0.40",
    "druckenmiller_macro_regime_analysis": "1.00",
    "compliance_anomaly_report":        "2.00",
    "compliance_bank_audit":            "2.00",
    "compliance_regulator_query":       "1.00",
    # --- Federal contracting (USAspending.gov / SAM.gov moat) ---
    "federal_contract_opportunities":   "0.50",
    "federal_award_history":            "0.35",
    "sdvosb_setaside_feed":             "0.75",
    "sam_entity_verification":          "0.40",
    "federal_spending_by_agency":       "0.30",
    "excluded_parties_check":           "0.25",
}

assert set(_PRICES_USD) == set(PROVIDER_ENDPOINTS), \
    f"x402_server price map drifted from provider.py ENDPOINTS — " \
    f"missing: {set(PROVIDER_ENDPOINTS) - set(_PRICES_USD)}, " \
    f"stale: {set(_PRICES_USD) - set(PROVIDER_ENDPOINTS)}"


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
    route = f"/x402/{name.replace('_', '-')}"
    @x402_guard(
        price_usd,
        f"scriptmasterlabs — {name.replace('_', ' ')}",
        discoverable=True,
        path=route,
        name=name,
    )
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


# ============================================================
# A2A + MCP DISCOVERABILITY INTEGRATION
# Added 2026-07-20 — makes all 48 endpoints autonomously discoverable
# by any A2A-scanning agent, MCP tool orchestrator, or x402 indexer.
# ============================================================
from a2a_agent_card import a2a_bp
from mcp_server import mcp_bp

app.register_blueprint(a2a_bp)
app.register_blueprint(mcp_bp)

# Routes now served:
#   GET /.well-known/agent.json  → A2A Protocol agent card (48 capabilities)
#   GET /.well-known/mcp.json    → MCP server manifest (48 tool definitions)
#   POST /mcp                     → MCP JSON-RPC 2.0 (initialize, tools/list, tools/call)
#   GET /mcp/sse                  → MCP SSE notification stream
#   GET /.well-known/x402        → x402 payment rails (existing, unchanged)


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
