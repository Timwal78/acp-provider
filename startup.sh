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

# Verify ACP is installed and working
echo "[startup] Verifying ACP CLI..."
acp --version || (echo "[startup] Installing ACP CLI..." && npm i -g @virtuals-protocol/acp-cli)
acp --version

echo "[startup] Verifying agent identity..."
acp agent whoami --json 2>&1 | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Agent: {d[\"name\"]} ({d[\"id\"]})')" 2>&1 || {
    echo "[startup] ERROR: Could not verify agent identity. Check ACP_ACCESS_TOKEN and ACP_REFRESH_TOKEN."
    exit 1
}

echo "[startup] Verifying signer..."
acp agent signer-policy --json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Signer policy: {d[\"policy\"]}')" 2>&1 || {
    echo "[startup] WARNING: Signer not available. Provider will run in listen-only mode."
}

echo "[startup] All checks passed. Launching provider..."
echo ""

# Launch the provider
cd /app
exec python3 provider.py
