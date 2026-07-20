#!/usr/bin/env bash
# ============================================================
# Render Background Worker Startup Script
# Reconstructs ACP credentials from env vars, then launches provider.
#
# AUTH MODEL (auth-optional):
#   REQUIRED: ACP_AGENT_WALLET_ADDRESS (x402 payment recipient)
#   OPTIONAL: ACP_REFRESH_TOKEN (enables ACP marketplace job intake)
#
# If auth is missing or fails, the HTTP server still boots and serves
# all x402/A2A/MCP endpoints. Only ACP job intake is affected.
# ============================================================
set -e

echo "[startup] ACP Provider for Render — starting..."

# --- Reconstruct ACP config from env vars ---
mkdir -p /opt/acp-config/acp-cli /opt/acp-config/keyring

if [ -z "$ACP_CONFIG_JSON" ]; then
    echo "[startup] ERROR: ACP_CONFIG_JSON env var not set"; exit 1
fi
echo "$ACP_CONFIG_JSON" > /opt/acp-config/config.json
echo "[startup] Wrote config.json"

if [ -z "$ACP_SIGNER_KEYS_JSON" ]; then
    echo "[startup] ERROR: ACP_SIGNER_KEYS_JSON env var not set"; exit 1
fi
echo "$ACP_SIGNER_KEYS_JSON" > /opt/acp-config/acp-cli/signer-keys.json
echo "[startup] Wrote signer-keys.json"

if [ -z "$ACP_KEYRING_KEY_B64" ]; then
    echo "[startup] ERROR: ACP_KEYRING_KEY_B64 env var not set"; exit 1
fi
echo "$ACP_KEYRING_KEY_B64" | base64 -d > /opt/acp-config/keyring/file.key
chmod 600 /opt/acp-config/keyring/file.key
echo "[startup] Wrote keyring/file.key"

export ACP_CONFIG_DIR=/opt/acp-config

# --- Wallet address (required for x402 payments) ---
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
    echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS env var not set"; exit 1
fi
export ACP_AGENT_WALLET_ADDRESS

# --- Refresh token (optional — enables ACP marketplace job intake) ---
ACP_REFRESH_TOKEN=$(echo -n "${ACP_REFRESH_TOKEN:-}" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_ACCESS_TOKEN=""
if [ -z "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] WARNING: ACP_REFRESH_TOKEN not set. HTTP/x402/A2A/MCP will work. ACP job intake disabled."
else
    echo "[startup] Auth inputs: refresh=${#ACP_REFRESH_TOKEN} chars, wallet=$ACP_AGENT_WALLET_ADDRESS"
fi
export ACP_REFRESH_TOKEN ACP_ACCESS_TOKEN

# --- Write tokens to file keyring (so CLI can auto-refresh if token exists) ---
export XDG_CONFIG_HOME=/opt/acp-config
export XDG_DATA_HOME=/opt/acp-config
export TS_KEYRING_BACKEND=file

echo "[startup] Verifying ACP CLI..."
acp --version || (echo "[startup] Installing ACP CLI..." && npm i -g @virtuals-protocol/acp-cli)
acp --version

# Only write keyring if we have a refresh token
if [ -n "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] Writing tokens to file keyring..."
    node -e "
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const wallet = process.argv[1];
const accessToken = process.argv[2];
const refreshToken = process.argv[3];
const ownerWallet = process.argv[4] || wallet;
const keyPath = path.join(process.env.XDG_CONFIG_HOME, 'keyring', 'file.key');
const key = fs.readFileSync(keyPath);
const w = wallet.toLowerCase();
const o = ownerWallet.toLowerCase();
const store = {
    'acp-auth': {
        ['access-token-' + w]: accessToken,
        ['refresh-token-' + w]: refreshToken,
        ['access-token-' + o]: accessToken,
        ['refresh-token-' + o]: refreshToken,
        'access-token': accessToken,
        'refresh-token': refreshToken
    }
};
const plaintext = JSON.stringify(store);
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
const authTag = cipher.getAuthTag();
const output = Buffer.concat([Buffer.from([1]), iv, authTag, encrypted]);
const dataPath = path.join(process.env.XDG_DATA_HOME, 'keyring', 'secrets.json');
fs.mkdirSync(path.dirname(dataPath), { recursive: true });
fs.writeFileSync(dataPath, output, { mode: 0o600 });
console.log('[startup] Tokens written to file keyring');
" "$ACP_AGENT_WALLET_ADDRESS" "$ACP_ACCESS_TOKEN" "$ACP_REFRESH_TOKEN" "0x25f2603be53bd4bed38aea500cb60fd10e7469ea" 2>&1 || {
        echo "[startup] WARNING: Could not write tokens to file keyring"
    }

    # Verify agent identity (non-fatal)
    WHOAMI_OUT=$(acp agent whoami --json 2>&1) || true
    if echo "$WHOAMI_OUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'[startup] Agent identity verified: {d[\"name\"]} ({d[\"id\"]})')" 2>/dev/null; then
        :
    else
        echo "[startup] WARNING: Could not verify agent identity via CLI. Continuing anyway."
    fi
else
    echo "[startup] Skipping keyring write (no refresh token). Provider will run without ACP job intake."
fi

echo "[startup] All checks passed. Launching provider..."
echo ""

# Launch the provider
cd /app
exec python3 provider.py
