# ScriptMasterLabs RWA API (ownable)

**Status:** engine LIVE + **x402 routes returning 402** on `acp-x402-scriptmasterlabs.onrender.com` (2026-07-22)

## What we own
- Curated asset registry (`rwa_engine.RWA_REGISTRY`)
- Valuation composite (TVL AUM proxy + market signals)
- Risk heuristic v1 (not a credit rating)
- Normalized agent JSON schema
- Endpoint surface on ACP + x402

## What we do NOT depend on
Paid RWA data vendors. Public feeds are raw inputs only (DefiLlama protocols, CoinGecko when available).

## Endpoints
| Name | Purpose | ACP target | x402 |
|------|---------|------------|------|
| `rwa_intelligence` | unified action=list\|valuation\|risk\|aggregates | $0.03 | $0.35 |
| `rwa_assets` | list/filter/sort | $0.02 | $0.25 |
| `rwa_valuation` | one asset valuation | $0.03 | $0.35 |
| `rwa_risk` | risk score | $0.02 | $0.25 |
| `rwa_aggregates` | class totals + top | $0.02 | $0.25 |

## Example
```bash
# local
python3 -c "from rwa_engine import aggregates; import json; print(json.dumps(aggregates({}), indent=2)[:800])"

# x402 (after deploy)
curl -sS https://acp-x402-scriptmasterlabs.onrender.com/x402/rwa-aggregates
# expects 402 payment required until paid
```

## ACP registration
Requires fresh CLI auth (`acp configure start` → complete). Then:

```bash
# free a low-demand slot if at 40 cap
acp offering delete --offering-id <epa_or_lobbying_id> --force --json

acp offering create   --name rwa_intelligence   --description "Ownable RWA intelligence — tokenized treasuries, credit, commodities. Valuation + risk + aggregates. action=list|valuation|risk|aggregates. No paid RWA vendor dependency."   --price-type fixed --price-value 0.03 --sla-minutes 5   --requirements '{"type":"object","properties":{"action":{"type":"string"},"id":{"type":"string"},"asset_class":{"type":"string"},"chain":{"type":"string"},"q":{"type":"string"},"min_tvl_usd":{"type":"string"},"limit":{"type":"string"}},"required":[]}'   --deliverable '{"type":"object","required":["result"],"properties":{"result":{"type":"string"}}}'   --no-required-funds --no-hidden --json
```

## Roadmap
1. Expand registry (USTB, BUIDL chain contracts, MAPLE pools)
2. Optional holder concentration via public RPC
3. Timeseries cache (sqlite/redis)
4. PoR attestation fields when issuers publish public proofs
5. Agent constraint query DSL (`yield>5`, `class=treasuries`, `risk<40`)

## Disclaimer
Informational only. Not audited NAV, not proof-of-reserves, not investment advice.
