#!/usr/bin/env bash
# ============================================================
# Render Background Worker Startup Script
# Reconstructs ACP credentials from env vars, then launches:
#   1) live_provider.mjs  — in-process setBudget + submit (SESSION_NOT_FOUND fix)
#   2) provider.py        — REST poll / logging / price cache
#
# AUTH MODEL:
#   REQUIRED: ACP_AGENT_WALLET_ADDRESS, ACP_CONFIG_JSON, ACP_SIGNER_KEYS_JSON, ACP_KEYRING_KEY_B64
#   OPTIONAL: ACP_REFRESH_TOKEN (job intake). ACP_ACCESS_TOKEN seed optional (minted from refresh).
# ============================================================
set -e
# note: individual ln may fail if same path; use || true where needed

echo "[startup] ACP Provider for Render — starting..."

# --- Reconstruct ACP config from env vars ---
mkdir -p /opt/acp-config/acp-cli /opt/acp-config/keyring /opt/acp-config/keyring

if [ -z "$ACP_CONFIG_JSON" ]; then
    echo "[startup] ERROR: ACP_CONFIG_JSON env var not set"; exit 1
fi
echo "$ACP_CONFIG_JSON" > /opt/acp-config/config.json
# Also expose where Node live_provider looks by default
cp /opt/acp-config/config.json /opt/acp-config/acp-cli/../config.json 2>/dev/null || true
echo "[startup] Wrote config.json"

if [ -z "$ACP_SIGNER_KEYS_JSON" ]; then
    echo "[startup] ERROR: ACP_SIGNER_KEYS_JSON env var not set"; exit 1
fi
echo "$ACP_SIGNER_KEYS_JSON" > /opt/acp-config/acp-cli/signer-keys.json
# CLI also reads ~/.config/acp-cli via XDG
mkdir -p /opt/acp-config/acp-cli
echo "$ACP_SIGNER_KEYS_JSON" > /opt/acp-config/acp-cli/signer-keys.json
echo "[startup] Wrote signer-keys.json"

if [ -z "$ACP_KEYRING_KEY_B64" ]; then
    echo "[startup] ERROR: ACP_KEYRING_KEY_B64 env var not set"; exit 1
fi
echo "$ACP_KEYRING_KEY_B64" | base64 -d > /opt/acp-config/keyring/file.key
chmod 600 /opt/acp-config/keyring/file.key
echo "[startup] Wrote keyring/file.key"

export ACP_CONFIG_DIR=/opt/acp-config
export ACP_CONFIG=/opt/acp-config/config.json
export XDG_CONFIG_HOME=/opt/acp-config
export XDG_DATA_HOME=/opt/acp-config
export TS_KEYRING_BACKEND=file
# Point acp-cli at reconstructed config + signer store
export HOME=/opt/acp-config/home
mkdir -p "$HOME/.config" "$HOME/.local/share"
mkdir -p "$HOME/.config/acp-cli" "$HOME/.config/keyring" "$HOME/.local/share/keyring" "$HOME/.config/acp"
ln -sfn /opt/acp-config/acp-cli/signer-keys.json "$HOME/.config/acp-cli/signer-keys.json" || true
ln -sfn /opt/acp-config/keyring/file.key "$HOME/.config/keyring/file.key" || true
ln -sfn /opt/acp-config/config.json "$HOME/.config/acp/config.json" || true
cp -f /opt/acp-config/config.json "$HOME/.config/acp/config.json" 2>/dev/null || true

# --- Wallet address ---
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
    echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS env var not set"; exit 1
fi
export ACP_AGENT_WALLET_ADDRESS

# --- Tokens: PRESERVE access seed from env; never wipe it ---
ACP_REFRESH_TOKEN=$(echo -n "${ACP_REFRESH_TOKEN:-}" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_ACCESS_TOKEN=$(echo -n "${ACP_ACCESS_TOKEN:-}" | tr -d '[:space:]' | tr -d '"' | tr -d "'")

# Normalize base64-wrapped JWT (chat paste artifact)
if [ -n "$ACP_ACCESS_TOKEN" ] && [[ "$ACP_ACCESS_TOKEN" != eyJ* ]]; then
    DECODED=$(printf '%s' "$ACP_ACCESS_TOKEN" | base64 -d 2>/dev/null || true)
    if [[ "$DECODED" == eyJ* ]]; then
        ACP_ACCESS_TOKEN="$DECODED"
        echo "[startup] Access token normalized from base64 → JWT (${#ACP_ACCESS_TOKEN} chars)"
    fi
fi

if [ -z "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] WARNING: ACP_REFRESH_TOKEN not set. Job intake disabled."
else
    echo "[startup] Auth inputs: refresh=${#ACP_REFRESH_TOKEN} chars, access_seed=${#ACP_ACCESS_TOKEN} chars, wallet=$ACP_AGENT_WALLET_ADDRESS"
fi

# Mint/refresh access if missing or expired (<10 min left)
if [ -n "$ACP_REFRESH_TOKEN" ]; then
    NEED_REFRESH=1
    if [[ "$ACP_ACCESS_TOKEN" == eyJ* ]]; then
        LEFT_MIN=$(python3 - <<'PY' "$ACP_ACCESS_TOKEN"
import sys,json,base64,time
tok=sys.argv[1]
p=tok.split('.')[1]
p += '=' * (-len(p)%4)
try:
    exp=json.loads(base64.urlsafe_b64decode(p)).get('exp') or 0
    print(int((exp-time.time())/60))
except Exception:
    print(-1)
PY
)
        if [ "$LEFT_MIN" -ge 10 ] 2>/dev/null; then
            NEED_REFRESH=0
            echo "[startup] Seed access still valid (left_min=$LEFT_MIN) — skip refresh"
        else
            echo "[startup] Seed access missing/expired (left_min=$LEFT_MIN) — refreshing"
        fi
    else
        echo "[startup] No valid access seed — refreshing via API"
    fi
    if [ "$NEED_REFRESH" = "1" ]; then
        RESP=$(curl -sS -X POST "https://api.acp.virtuals.io/auth/cli/refresh" \
            -H "Content-Type: application/json" -H "User-Agent: Mozilla/5.0" \
            -d "{\"refreshToken\":\"$ACP_REFRESH_TOKEN\"}" || true)
        NEW_ACCESS=$(printf '%s' "$RESP" | python3 -c 'import sys,json;d=json.load(sys.stdin);x=d.get("data") or d;print(x.get("token") or x.get("accessToken") or "")' 2>/dev/null || true)
        NEW_REFRESH=$(printf '%s' "$RESP" | python3 -c 'import sys,json;d=json.load(sys.stdin);x=d.get("data") or d;print(x.get("refreshToken") or "")' 2>/dev/null || true)
        if [[ "$NEW_ACCESS" == eyJ* ]]; then
            ACP_ACCESS_TOKEN="$NEW_ACCESS"
            if [ -n "$NEW_REFRESH" ]; then
                ACP_REFRESH_TOKEN="$NEW_REFRESH"
                echo "[startup] Refresh OK — access=${#ACP_ACCESS_TOKEN} chars, refresh rotated (update Render env when convenient)"
            else
                echo "[startup] Refresh OK — access=${#ACP_ACCESS_TOKEN} chars"
            fi
        else
            echo "[startup] WARNING: refresh failed: ${RESP:0:200}"
        fi
    fi
fi

export ACP_REFRESH_TOKEN ACP_ACCESS_TOKEN

echo "[startup] Verifying ACP CLI..."
acp --version || (echo "[startup] Installing ACP CLI..." && npm i -g @virtuals-protocol/acp-cli)
acp --version

# Write tokens to file keyring for CLI + provider.py
if [ -n "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] Writing tokens to file keyring..."
    node -e "
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const wallet = process.argv[1];
const accessToken = process.argv[2] || '';
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
// also under HOME layout
const homeData = path.join(process.env.HOME || '', '.local/share/keyring/secrets.json');
if (process.env.HOME) {
  fs.mkdirSync(path.dirname(homeData), { recursive: true });
  fs.writeFileSync(homeData, output, { mode: 0o600 });
}
console.log('[startup] Tokens written to file keyring (access=' + (accessToken||'').length + ' chars)');
" "$ACP_AGENT_WALLET_ADDRESS" "$ACP_ACCESS_TOKEN" "$ACP_REFRESH_TOKEN" "0x25f2603be53bd4bed38aea500cb60fd10e7469ea" 2>&1 || {
        echo "[startup] WARNING: Could not write tokens to file keyring"
    }

    WHOAMI_OUT=$(acp agent whoami --json 2>&1) || true
    if echo "$WHOAMI_OUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'[startup] Agent identity verified: {d.get(\"name\")} ({d.get(\"id\")})')" 2>/dev/null; then
        :
    else
        echo "[startup] WARNING: whoami failed (continuing). out=${WHOAMI_OUT:0:160}"
    fi
else
    echo "[startup] Skipping keyring write (no refresh token)."
fi

# Install acp-node-v2 deps for live_provider if package.json present
if [ -f /app/package.json ]; then
    echo "[startup] Ensuring node deps for live_provider..."
    (cd /app && npm install --omit=dev --no-audit --no-fund 2>&1 | tail -5) || echo "[startup] WARNING: npm install failed"
fi

echo "[startup] All checks passed. Launching live_provider + provider..."
echo ""

cd /app

# Locate signer binary (npm global package layout)
ACP_SIGNER_BIN=""
for cand in   "$(npm root -g 2>/dev/null)/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux"   /usr/local/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux   /usr/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux
do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    ACP_SIGNER_BIN="$cand"
    break
  fi
done
if [ -n "$ACP_SIGNER_BIN" ]; then
  export ACP_SIGNER_BIN
  echo "[startup] ACP_SIGNER_BIN=$ACP_SIGNER_BIN"
else
  echo "[startup] WARNING: acp-cli-signer-linux not found — live_provider will fail setBudget"
fi

# Live in-process agent (setBudget + submit). Critical — CLI one-shot hits SESSION_NOT_FOUND.
# STDOUT/STDERR unredirected so Render log drain captures boot/sse/setBudget evidence.
if [ -f /app/live_provider.mjs ]; then
    # stdbuf line-buffer if available so logs flush immediately
    if command -v stdbuf >/dev/null 2>&1; then
      stdbuf -oL -eL node /app/live_provider.mjs &
    else
      node /app/live_provider.mjs &
    fi
    LIVE_PID=$!
    echo "[startup] live_provider.mjs pid $LIVE_PID (stdout→Render logs)"
    # brief wait + liveness check
    sleep 2
    if kill -0 "$LIVE_PID" 2>/dev/null; then
      echo "[startup] live_provider still running after 2s"
    else
      echo "[startup] ERROR: live_provider exited early — check FATAL lines above"
    fi
else
    echo "[startup] WARNING: live_provider.mjs missing — setBudget will fail on CLI path"
fi

exec python3 provider.py
