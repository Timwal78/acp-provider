# ACP Provider Server — scriptmasterlabs

Background worker that sells 18 API endpoints on the Virtuals Protocol ACP marketplace.

## Files in This Repo

| File | Purpose |
|---|---|
| `provider.py` | Main server — 18 API endpoints, 1,308 lines |
| `startup.sh` | Reconstructs ACP credentials from env vars, launches provider |
| `Dockerfile` | Python 3.11 + Node.js 18 + ACP CLI |
| `render.yaml` | Render service definition |
| `package.json` | Node.js metadata |
| `.gitignore` | Excludes secrets, keys, pycache |

## Step 1: Push to GitHub

Create a new GitHub repo (e.g. `acp-provider`), then push all files from `/workspace/acp-render/`:

```bash
cd /workspace/acp-render
git remote add origin https://github.com/YOUR_USERNAME/acp-provider.git
git push -u origin master
```

## Step 2: Deploy on Render

1. Go to **render.com** → **New** → **Background Worker**
2. Connect your GitHub account and select the `acp-provider` repo
3. Settings:
   - **Runtime**: Docker
   - **Dockerfile Path**: `./Dockerfile`
   - **Plan**: Starter ($7/mo)
4. Add these **Environment Variables** (copy-paste each value exactly):

### `ACP_CONFIG_JSON`
```
{"activeWallet":"0x72330994f379a71542e7bd5a4cf99a9d9743f4aa","agents":{"0x72330994f379a71542e7bd5a4cf99a9d9743f4aa":{"id":"019f5f40-c194-7776-b5e1-7a666ce631c0","publicKey":"MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuEhRLPb6V2+8Muq+T+P2AQej0ztDzu4faXRXC+haeoEj80hz69JN3xlazGa75BT0RKS01t+oI8AeV/Sqa7gqvg==","walletId":"odh4czkyd34w3bgtqzjw1ag9","builderCode":"bc_0gi3t7qi"}}}
```

### `ACP_SIGNER_KEYS_JSON`
```
{"secret":"90f85e14d94762b783e4581e1a0c913665ab5a4538f55fb54b81a2aaf9cbf17a","salt":"1500155606f6c7d5791782e32b0a1a1a","keys":{"MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuEhRLPb6V2+8Muq+T+P2AQej0ztDzu4faXRXC+haeoEj80hz69JN3xlazGa75BT0RKS01t+oI8AeV/Sqa7gqvg==":{"nonce":"0f600f12ba3b587388927b2f","ciphertext":"814bd3c8ab9121a66c97180eddddf39e47915b995bb9807d77d43686757615d18596483ceb4b4503b074ba09e2016fd0"}}}
```

### `ACP_KEYRING_KEY_B64`
```
cMA6LAYeXjsLDzo85UN+/SDJEAqRD3MgS+fW+pVbhVs=
```

### `PYTHONUNBUFFERED`
```
1
```

5. Click **Create Worker** and wait for the build to complete
6. Check the logs — you should see:
   ```
   [startup] ACP Provider for Render — starting...
   [startup] All checks passed. Launching provider...
   [INFO] Endpoints: ['perp_funding_aggregator', 'market_regime_indicator', ...]
   [INFO] Event listener running. Entering main loop...
   ```

## 18 API Endpoints

### Crypto/DeFi (7) — Data from Hyperliquid, CoinGecko, DefiLlama, Fear&Greed
| Endpoint | Price | Description |
|---|---|---|
| `perp_funding_aggregator` | $0.50 | Funding rates, OI, mark prices across 232 perp markets |
| `market_regime_indicator` | $0.50 | Fear & Greed, regime classification, BTC dominance |
| `defi_yield_rates` | $0.30 | Top yield pools by APY from 15,000+ pools |
| `defi_tvl_ranking` | $0.30 | Protocol TVL rankings, 7,800+ protocols |
| `crypto_market_overview` | $0.20 | Top coins by market cap |
| `crypto_price_lookup` | $0.15 | Real-time prices for any token |
| `stablecoin_flow_tracker` | $0.25 | USDT/USDC/DAI market cap, supply, DEX volume |

### Federal/Contracting (6) — SAM.gov moat (UEI G24VZA4RLMK3)
| Endpoint | Price | Description |
|---|---|---|
| `federal_contract_opportunities` | $0.50 | Active federal contract awards from USAspending.gov |
| `federal_award_history` | $0.35 | Contract award history by contractor name |
| `sdvosb_setaside_feed` | $0.75 | SDVOSB/VOSB set-aside contract awards |
| `sam_entity_verification` | $0.40 | Verify federal contractor entity (UEI, DUNS) |
| `federal_spending_by_agency` | $0.30 | Federal spending breakdown by agency |
| `excluded_parties_check` | $0.25 | Entity exclusion/debarment check |

### Crypto On-Chain Analytics (5) — Data from DexScreener, GoPlus Labs
| Endpoint | Price | Description |
|---|---|---|
| `crypto_onchain_analytics` | $0.40 | Token price, volume, liquidity, FDV, txns |
| `crypto_sentiment_scanner` | $0.35 | F&G + token volume sentiment |
| `dex_volume_ranking` | $0.25 | DEX protocol volume rankings |
| `token_security_audit` | $0.30 | Honeypot/rugpull risk, taxes, ownership |
| `whale_wallet_tracker` | $0.40 | High-volume pair tracking |

## Data Sources (all free, no API keys needed)
- **Hyperliquid API** — funding rates, open interest
- **CoinGecko API** — prices, market cap, global data
- **DefiLlama API** — yields, TVL, DEX volumes
- **Fear & Greed Index** — sentiment
- **USAspending.gov API** — federal contract data
- **DexScreener API** — DEX pair data
- **GoPlus Labs API** — token security audits

## Agent Identity
- **Agent name**: scriptmasterlabs
- **Agent ID**: 019f5f40-c194-7776-b5e1-7a666ce631c0
- **Wallet**: 0x72330994f379a71542e7bd5a4cf99a9d9743f4aa
- **Chain**: Base (8453)
- **Token**: SCRIPT
- **Signer**: dyfod3rab0y6ahpra274ud70 (No Policy — unrestricted)
- **Subscription**: $49/mo starter (UUID 019f61dd-1819-79b8-99ca-f4e18558be4f)

## Marketplace Structure (40 offerings)
- 34 offerings at $0.03–$0.75 with $49/mo subscription
- 6 premium offerings at $0.30–$5.00 (pay-per-call, no subscription)
