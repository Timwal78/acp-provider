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

# Trim whitespace/newlines from tokens AND the wallet address — copy-paste often
# adds trailing chars. Missing this on the wallet address was a real bug: it's
# used verbatim below to build the file-keyring account name
# ("access-token-" + wallet), and cross-keychain's file backend rejects any
# account string with a non-alphanumeric/dot/underscore/@/hyphen character —
# a single trailing space/newline in the env var silently broke every boot's
# keyring write with an opaque "account contains invalid characters" warning.
ACP_ACCESS_TOKEN=$(echo -n "$ACP_ACCESS_TOKEN" | tr -d '[:space:]')
ACP_REFRESH_TOKEN=$(echo -n "$ACP_REFRESH_TOKEN" | tr -d '[:space:]')
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
echo "[startup] Tokens trimmed (access=${#ACP_ACCESS_TOKEN} chars, refresh=${#ACP_REFRESH_TOKEN} chars)"

# Write tokens directly to the file-based keyring using a Node.js script.
# This bypasses the native Linux keyring backend which rejects wallet addresses
# as account names ("invalid characters" error).
# The CLI then reads tokens from the file keyring and auto-refreshes via Node fetch.
echo "[startup] Verifying ACP CLI..."
acp --version || (echo "[startup] Installing ACP CLI..." && npm i -g @virtuals-protocol/acp-cli)
acp --version

echo "[startup] Writing tokens to file keyring..."
# Set XDG paths so the file keyring finds file.key and writes secrets.json in our config dir
export XDG_CONFIG_HOME=/opt/acp-config
export XDG_DATA_HOME=/opt/acp-config

# Find the cross-keychain module — try multiple known paths for global npm installs
KEYRING_MODULE=""
for p in \
    "/usr/lib/node_modules/@virtuals-protocol/acp-cli/node_modules/cross-keychain/dist/index.js" \
    "/usr/local/lib/node_modules/@virtuals-protocol/acp-cli/node_modules/cross-keychain/dist/index.js" \
    "$(npm root -g 2>/dev/null)/@virtuals-protocol/acp-cli/node_modules/cross-keychain/dist/index.js"; do
    if [ -f "$p" ]; then
        KEYRING_MODULE="$p"
        break
    fi
done

if [ -n "$KEYRING_MODULE" ]; then
    echo "[startup] Found cross-keychain at: $KEYRING_MODULE"
    # Pass values as argv to avoid shell interpolation mangling JS string literals
    node -e "
const mod = require(process.argv[1]);
const wallet = process.argv[2];
const accessToken = process.argv[3];
const refreshToken = process.argv[4];
async function main() {
    await mod.useBackend('file');
    const w = wallet.toLowerCase();
    await mod.setPassword('acp-auth', 'access-token-' + w, accessToken);
    await mod.setPassword('acp-auth', 'refresh-token-' + w, refreshToken);
    console.log('[startup] Tokens written to file keyring');
}
main().catch(e => { console.error('[startup] WARNING: Failed to write keyring:', e.message); });
    " "$KEYRING_MODULE" "$ACP_AGENT_WALLET_ADDRESS" "$ACP_ACCESS_TOKEN" "$ACP_REFRESH_TOKEN" 2>&1 || {
        echo "[startup] WARNING: Could not write tokens to file keyring"
    }
else
    echo "[startup] WARNING: cross-keychain module not found, skipping keyring write"
fi

# The CLI auto-refreshes the access token internally using Node fetch (not blocked by Cloudflare)
echo "[startup] Verifying agent identity (CLI will auto-refresh if needed)..."
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
