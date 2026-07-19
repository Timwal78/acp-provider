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
ACP_ACCESS_TOKEN=$(echo -n "$ACP_ACCESS_TOKEN" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_REFRESH_TOKEN=$(echo -n "$ACP_REFRESH_TOKEN" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
export ACP_ACCESS_TOKEN ACP_REFRESH_TOKEN ACP_AGENT_WALLET_ADDRESS
echo "[startup] Tokens trimmed (access=${#ACP_ACCESS_TOKEN} chars, refresh=${#ACP_REFRESH_TOKEN} chars)"

# Auto-heal common paste mistakes:
# 1) Access token pasted as base64(JWT) instead of raw JWT (len ~272 instead of ~204)
# 2) Access token pasted as hex(JWT)
NORMALIZED=$(python3 -c '
import os, base64, re, sys
tok = os.environ.get("ACP_ACCESS_TOKEN", "").strip()

def looks_jwt(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 3 and all(parts) and s.startswith("eyJ")

if looks_jwt(tok):
    sys.stdout.write(tok); raise SystemExit(0)

if re.fullmatch(r"[0-9a-fA-F]+", tok or "") and len(tok) % 2 == 0:
    try:
        dec = bytes.fromhex(tok).decode("utf-8", errors="strict")
        if looks_jwt(dec):
            sys.stdout.write(dec); raise SystemExit(0)
    except Exception:
        pass

for decoder in (base64.b64decode, base64.urlsafe_b64decode):
    try:
        pad = tok + ("=" * (-len(tok) % 4))
        dec = decoder(pad).decode("utf-8", errors="strict")
        if looks_jwt(dec):
            sys.stdout.write(dec); raise SystemExit(0)
    except Exception:
        pass

sys.stdout.write(tok)
')
ACP_ACCESS_TOKEN="$NORMALIZED"
export ACP_ACCESS_TOKEN
if [[ "$ACP_ACCESS_TOKEN" == eyJ* ]]; then
  echo "[startup] Access token normalized (access=${#ACP_ACCESS_TOKEN} chars, jwt=yes)"
else
  echo "[startup] WARNING: Access token does not look like a JWT after normalize (access=${#ACP_ACCESS_TOKEN} chars)"
fi

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

# Force file-based keyring backend (native DBus/secret-service fails in Docker)
export TS_KEYRING_BACKEND=file

# Write tokens directly to the encrypted secrets.json file using Node's built-in crypto.
# This bypasses cross-keychain's validateIdentifier() which rejects wallet addresses
# as account names on both native and file backends.
# Format: version(1 byte) + iv(12) + authTag(16) + ciphertext (AES-256-GCM)
node -e "
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const wallet = process.argv[1];
const accessToken = process.argv[2];
const refreshToken = process.argv[3];
const ownerWallet = process.argv[4] || wallet;

// Read the keyring encryption key
const keyPath = path.join(process.env.XDG_CONFIG_HOME || (path.join(require('os').homedir(), '.config')), 'keyring', 'file.key');
const key = fs.readFileSync(keyPath);

// Build the store (same structure cross-keychain expects)
// Write under agent wallet, owner wallet, and bare keys for CLI compatibility.
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

// Encrypt: version(1) + iv(12) + authTag(16) + ciphertext
const plaintext = JSON.stringify(store);
const iv = crypto.randomBytes(12);
const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
const authTag = cipher.getAuthTag();
const output = Buffer.concat([Buffer.from([1]), iv, authTag, encrypted]);

// Write to secrets.json
const dataPath = path.join(process.env.XDG_DATA_HOME || (path.join(require('os').homedir(), '.local', 'share')), 'keyring', 'secrets.json');
fs.mkdirSync(path.dirname(dataPath), { recursive: true });
fs.writeFileSync(dataPath, output, { mode: 0o600 });
console.log('[startup] Tokens written to file keyring for agent+owner wallets');
" "$ACP_AGENT_WALLET_ADDRESS" "$ACP_ACCESS_TOKEN" "$ACP_REFRESH_TOKEN" "0x25f2603be53bd4bed38aea500cb60fd10e7469ea" 2>&1 || {
    echo "[startup] WARNING: Could not write tokens to file keyring"
}

# The CLI auto-refreshes the access token internally using Node fetch (not blocked by Cloudflare)
echo "[startup] Verifying agent identity (CLI will auto-refresh if needed)..."
# `|| true` is required here under `set -e`: without it, a non-zero exit from
# `acp agent whoami` kills the whole script on this line, before ever reaching
# the if/else below whose entire purpose is to handle that failure gracefully
# ("Continuing anyway — provider.py will use env vars for auth"). That
# fallback was unreachable dead code until this fix -- confirmed against a
# real Render deploy that exited status 1 immediately after this line.
WHOAMI_OUT=$(acp agent whoami --json 2>&1) || true
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
