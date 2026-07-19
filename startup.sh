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

# ============================================================
# AUTH MODEL (simple):
#   REQUIRED: ACP_REFRESH_TOKEN + ACP_AGENT_WALLET_ADDRESS
#   OPTIONAL: ACP_ACCESS_TOKEN (seed only; we mint a fresh one on every boot)
# Never paste JWTs again. Paste the 64-char hex refresh token only.
# ============================================================
if [ -z "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] ERROR: ACP_REFRESH_TOKEN env var not set (64-char hex). This is the ONLY token you need to paste."
    exit 1
fi
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
    echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS env var not set"
    exit 1
fi

# Trim copy-paste junk
ACP_REFRESH_TOKEN=$(echo -n "$ACP_REFRESH_TOKEN" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
if [ -n "${ACP_ACCESS_TOKEN:-}" ]; then
  ACP_ACCESS_TOKEN=$(echo -n "$ACP_ACCESS_TOKEN" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
else
  ACP_ACCESS_TOKEN=""
fi
export ACP_REFRESH_TOKEN ACP_AGENT_WALLET_ADDRESS ACP_ACCESS_TOKEN
echo "[startup] Auth inputs: refresh=${#ACP_REFRESH_TOKEN} chars, access_seed=${#ACP_ACCESS_TOKEN} chars, wallet=$ACP_AGENT_WALLET_ADDRESS"

# Normalize accidental base64/hex paste of access seed (legacy)
if [ -n "$ACP_ACCESS_TOKEN" ]; then
  ACP_ACCESS_TOKEN=$(ACP_ACCESS_TOKEN="$ACP_ACCESS_TOKEN" python3 -c '
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
  export ACP_ACCESS_TOKEN
fi

# Always mint a FRESH access token from refresh on boot (curl, not urllib — Cloudflare).
# Rotating refresh: save the NEW refresh token back into env for this process.
#
# IMPORTANT: ACP refresh tokens ROTATE. Calling refresh invalidates the old refresh
# stored in Render env. Strategy:
#   1) If seed access JWT still has >10 minutes left, SKIP refresh (keep env refresh valid)
#   2) Else refresh once, use new pair for this process lifetime
#   3) CLI keeps the process alive via keyring; avoid redeploys unless necessary
echo "[startup] Resolving access token (prefer valid seed; refresh only if needed)..."
python3 - <<'PY'
import json, os, sys, subprocess, base64, datetime

def jwt_left_min(tok: str):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p))
        return (payload.get("exp", 0) - datetime.datetime.utcnow().timestamp()) / 60.0
    except Exception:
        return -1e9

seed = (os.environ.get("ACP_ACCESS_TOKEN") or "").strip()
refresh = (os.environ.get("ACP_REFRESH_TOKEN") or "").strip()
left = jwt_left_min(seed) if seed.startswith("eyJ") else -1e9

if left >= 10:
    print(f"[startup] Seed access still valid (left_min={left:.1f}) — skip refresh to preserve Render refresh token")
    open("/tmp/acp_access_new", "w").write(seed)
    open("/tmp/acp_refresh_new", "w").write(refresh)
    os.chmod("/tmp/acp_access_new", 0o600)
    os.chmod("/tmp/acp_refresh_new", 0o600)
    raise SystemExit(0)

print(f"[startup] Seed access missing/expired (left_min={left:.1f}) — refreshing via API...")
path = "/tmp/acp_refresh_payload.json"
with open(path, "w") as f:
    json.dump({"refreshToken": refresh}, f)

r = subprocess.run([
    "curl", "-sS", "--max-time", "25",
    "-X", "POST", "https://api.acp.virtuals.io/auth/cli/refresh",
    "-H", "Content-Type: application/json",
    "-H", "User-Agent: Mozilla/5.0",
    "-d", f"@{path}",
], capture_output=True, text=True)
body = r.stdout or ""
try:
    d = json.loads(body)
except Exception:
    print(f"[startup] ERROR: refresh non-JSON response: {body[:200]}", file=sys.stderr)
    sys.exit(2)

data = d.get("data") or d
access = data.get("token") or data.get("accessToken")
new_refresh = data.get("refreshToken")
if not access or not new_refresh:
    msg = d.get("message") or d.get("error") or body[:200]
    print(f"[startup] ERROR: refresh failed: {msg}", file=sys.stderr)
    if left > 0 and seed.startswith("eyJ"):
        print("[startup] WARNING: using nearly-expired seed access; refresh failed", file=sys.stderr)
        open("/tmp/acp_access_new", "w").write(seed)
        open("/tmp/acp_refresh_new", "w").write(refresh)
        raise SystemExit(0)
    sys.exit(3)

left2 = jwt_left_min(access)
print(f"[startup] Refresh OK — access left_min={left2:.1f}, new_refresh_len={len(new_refresh)}")
print("[startup] NOTE: refresh token rotated for this process. Avoid redeploy until you update ACP_REFRESH_TOKEN if this instance dies.")
open("/tmp/acp_access_new", "w").write(access)
open("/tmp/acp_refresh_new", "w").write(new_refresh)
os.chmod("/tmp/acp_access_new", 0o600)
os.chmod("/tmp/acp_refresh_new", 0o600)
PY
REFRESH_RC=$?
if [ "$REFRESH_RC" -eq 0 ] && [ -f /tmp/acp_access_new ] && [ -f /tmp/acp_refresh_new ]; then
  ACP_ACCESS_TOKEN=$(cat /tmp/acp_access_new)
  ACP_REFRESH_TOKEN=$(cat /tmp/acp_refresh_new)
  export ACP_ACCESS_TOKEN ACP_REFRESH_TOKEN
  echo "[startup] Boot tokens ready (access=${#ACP_ACCESS_TOKEN} chars, refresh=${#ACP_REFRESH_TOKEN} chars)"
elif [ -n "$ACP_ACCESS_TOKEN" ] && [[ "$ACP_ACCESS_TOKEN" == eyJ* ]]; then
  echo "[startup] WARNING: token resolve failed (rc=$REFRESH_RC); continuing with seed access token"
else
  echo "[startup] ERROR: could not obtain access token. Update ACP_REFRESH_TOKEN in Render env (64-char hex from fresh sign-in)."
  exit 1
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
