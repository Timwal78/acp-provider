#!/usr/bin/env bash
# ============================================================
# Render Background Worker Startup Script
# Reconstructs ACP credentials from env vars, then launches provider
# ============================================================
set -e

echo "[startup] ACP Provider for Render — starting..."

# --- Reconstruct ACP config ---
mkdir -p /opt/acp-config
mkdir -p /opt/acp-config/acp-cli
mkdir -p /opt/acp-config/keyring

# Write the ACP config.json from env var
if [ -z "$ACP_CONFIG_JSON" ]; then
    echo "[startup] ERROR: ACP_CONFIG_JSON env var not set"
    exit 1
fi
echo "$ACP_CONFIG_JSON" > /opt/acp-config/config.json
echo "[startup] Wrote config.json"

# Write the signer keys from env var
if [ -z "$ACP_SIGNER_KEYS_JSON" ]; then
    echo "[startup] ERROR: ACP_SIGNER_KEYS_JSON env var not set"
    exit 1
fi
echo "$ACP_SIGNER_KEYS_JSON" > /opt/acp-config/acp-cli/signer-keys.json
echo "[startup] Wrote signer-keys.json"

# Write the keyring file key from env var (base64 encoded)
if [ -z "$ACP_KEYRING_KEY_B64" ]; then
    echo "[startup] ERROR: ACP_KEYRING_KEY_B64 env var not set"
    exit 1
fi
echo "$ACP_KEYRING_KEY_B64" | base64 -d > /opt/acp-config/keyring/file.key
chmod 600 /opt/acp-config/keyring/file.key
echo "[startup] Wrote keyring/file.key"

# Point ACP_CONFIG_DIR at our config dir
export ACP_CONFIG_DIR=/opt/acp-config

# The ACP CLI authenticates via JWT tokens in env vars, NOT config files.
# These must be set for `acp agent whoami` to work.
if [ -z "$ACP_ACCESS_TOKEN" ]; then
    echo "[startup] ERROR: ACP_ACCESS_TOKEN env var not set"
    exit 1
fi
if [ -z "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] ERROR: ACP_REFRESH_TOKEN env var not set"
    exit 1
fi
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
    echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS env var not set"
    exit 1
fi
echo "[startup] Auth tokens present (access=${#ACP_ACCESS_TOKEN} chars, refresh=${#ACP_REFRESH_TOKEN} chars)"

# Attempt to refresh the access token using the refresh token.
# The access token expires every hour, so on Render restarts it will almost always be stale.
# The refresh token lasts longer. We call the ACP API directly to get a fresh access token.
echo "[startup] Refreshing access token..."
# Write JSON payload to a temp file using python3 (guarantees valid JSON, no shell escaping issues)
# Then use curl -d @file (bypasses Cloudflare's Python urllib block)
python3 -c "import json,os; json.dump({'refreshToken': os.environ.get('ACP_REFRESH_TOKEN','')}, open('/tmp/refresh_payload.json','w'))"
REFRESH_RESULT=$(curl -s --max-time 10 -X POST "https://api.acp.virtuals.io/auth/cli/refresh" \
    -H "Content-Type: application/json" \
    -d @/tmp/refresh_payload.json 2>&1)
rm -f /tmp/refresh_payload.json

# Parse the JSON response with Python (just parsing, no network call)
if echo "$REFRESH_RESULT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert 'data' in d and 'token' in d['data']
" 2>/dev/null; then
    export ACP_ACCESS_TOKEN=$(echo "$REFRESH_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['token'])")
    NEW_RT=$(echo "$REFRESH_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'].get('refreshToken',''))")
    if [ -n "$NEW_RT" ]; then
        export ACP_REFRESH_TOKEN="$NEW_RT"
    fi
    echo "[startup] Token refreshed successfully (new access=${#ACP_ACCESS_TOKEN} chars)"
else
    echo "[startup] WARNING: Token refresh failed: $(echo "$REFRESH_RESULT" | head -c 150)"
    echo "[startup] Continuing with existing token — CLI may auto-refresh if refresh token is still valid."
fi

# Verify ACP is installed and working
echo "[startup] Verifying ACP CLI..."
acp --version || (echo "[startup] Installing ACP CLI..." && npm i -g @virtuals-protocol/acp-cli)
acp --version

echo "[startup] Verifying agent identity..."
WHOAMI_OUT=$(acp agent whoami --json 2>&1)
if echo "$WHOAMI_OUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Agent: {d[\"name\"]} ({d[\"id\"]})')" 2>/dev/null; then
    echo "[startup] Agent identity verified."
else
    echo "[startup] WARNING: Could not verify agent identity via CLI."
    echo "[startup] Output: $(echo "$WHOAMI_OUT" | head -c 200)"
    echo "[startup] Continuing anyway — provider.py will use env vars for auth."
fi

echo "[startup] Verifying signer..."
acp agent signer-policy --json 2>&1 | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Signer policy: {d[\"policy\"]}')" 2>&1 || {
    echo "[startup] WARNING: Signer not available via CLI. Provider will handle signing internally."
}

echo "[startup] All checks passed. Launching provider..."
echo ""

# Launch the provider
cd /app
exec python3 provider.py
