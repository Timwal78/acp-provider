# x402scan discovery fix (2026-07-22)

## Why nothing showed on x402scan
`x402_guard` recorded `fn.__name__` into DISCOVERY. Every route was a nested `_view`, so
`/.well-known/x402` advertised:

```json
{"path": "_view", "price": ...}
```

for all ~49 endpoints. Scanners cannot resolve `_view` → invisible catalog.

## Fix
1. Pass explicit `path=/x402/<hyphen-name>` + `name=` into `x402_guard`.
2. Emit OpenAPI 3.1 discovery (same shape as live `mcp-x402.onrender.com/.well-known/x402`) with:
   - `paths./x402/...get.x-payment-info`
   - `resources[]` with real paths + absolute URLs
   - `servers`, `x-service-info`
3. Aliases: `/openapi.json`, `/x402/openapi.json`

## Verify
```bash
curl -sS https://acp-x402-scriptmasterlabs.onrender.com/.well-known/x402 | jq '.paths | keys[:5]'
# expect /x402/... not _view
curl -sS -o /dev/null -w "%{http_code}\n" https://acp-x402-scriptmasterlabs.onrender.com/x402/rwa-aggregates
# 402
```

## Indexer lag
x402scan may take minutes–hours to recrawl. Discovery doc is correct now; payment routes were already 402-live before the catalog bug fix.
