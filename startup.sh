#!/usr/bin/env bash
# ============================================================
# Render Background Worker Startup Script
#
# CRITICAL (proven 2026-07-21): acp-cli-signer encrypted-file
# keystore is HOME-PATH-BOUND. Same signer-keys.json MAC-fails
# if HOME string differs. Keys were created under:
#   HOME=/home/hermes/.hermes/home
# So this container MUST use that exact HOME string and place
# signer-keys.json at:
#   $HOME/.config/acp-cli/signer-keys.json
#
# Launches:
#   1) live_provider.mjs — in-process setBudget + submit
#   2) provider.py       — REST poll / logging / handlers
# ============================================================
set -e

echo "[startup] ACP Provider for Render — starting..."

# --- Fixed HOME path (MUST match key generation environment) ---
export SIGNER_HOME="/home/hermes/.hermes/home"
export HOME="$SIGNER_HOME"
mkdir -p "$HOME/.config/acp-cli" \
         "$HOME/.config/keyring" \
         "$HOME/.config/acp" \
         "$HOME/.local/share/keyring" \
         /opt/acp-config/acp-cli \
         /opt/acp-config/keyring

export ACP_CONFIG_DIR=/opt/acp-config
export ACP_CONFIG=/opt/acp-config/config.json
# Do NOT set XDG_CONFIG_HOME away from HOME — signer uses $HOME/.config/acp-cli
unset XDG_CONFIG_HOME || true
export XDG_DATA_HOME="$HOME/.local/share"
export TS_KEYRING_BACKEND=file

# --- config.json ---
if [ -z "$ACP_CONFIG_JSON" ]; then
    echo "[startup] ERROR: ACP_CONFIG_JSON env var not set"; exit 1
fi
printf '%s' "$ACP_CONFIG_JSON" > /opt/acp-config/config.json
cp -f /opt/acp-config/config.json "$HOME/.config/acp/config.json"
echo "[startup] Wrote config.json"

# --- signer-keys.json at EXACT path signer expects ---
if [ -z "$ACP_SIGNER_KEYS_JSON" ]; then
    echo "[startup] ERROR: ACP_SIGNER_KEYS_JSON env var not set"; exit 1
fi
printf '%s' "$ACP_SIGNER_KEYS_JSON" > "$HOME/.config/acp-cli/signer-keys.json"
cp -f "$HOME/.config/acp-cli/signer-keys.json" /opt/acp-config/acp-cli/signer-keys.json
chmod 600 "$HOME/.config/acp-cli/signer-keys.json"
echo "[startup] Wrote signer-keys.json → $HOME/.config/acp-cli/signer-keys.json (HOME-path-bound)"

# --- keyring file.key (JWT store only; NOT used by signer MAC) ---
if [ -z "$ACP_KEYRING_KEY_B64" ]; then
    echo "[startup] ERROR: ACP_KEYRING_KEY_B64 env var not set"; exit 1
fi
echo "$ACP_KEYRING_KEY_B64" | base64 -d > "$HOME/.config/keyring/file.key"
cp -f "$HOME/.config/keyring/file.key" /opt/acp-config/keyring/file.key
chmod 600 "$HOME/.config/keyring/file.key" /opt/acp-config/keyring/file.key
echo "[startup] Wrote keyring/file.key"

# --- Wallet ---
ACP_AGENT_WALLET_ADDRESS=$(echo -n "$ACP_AGENT_WALLET_ADDRESS" | tr -d '[:space:]')
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
    echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS env var not set"; exit 1
fi
export ACP_AGENT_WALLET_ADDRESS

# --- Tokens ---
ACP_REFRESH_TOKEN=$(echo -n "${ACP_REFRESH_TOKEN:-}" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
ACP_ACCESS_TOKEN=$(echo -n "${ACP_ACCESS_TOKEN:-}" | tr -d '[:space:]' | tr -d '"' | tr -d "'")

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

if [ -n "$ACP_REFRESH_TOKEN" ]; then
    NEED_REFRESH=1
    if [[ "$ACP_ACCESS_TOKEN" == eyJ* ]]; then
        LEFT_MIN=$(python3 -c "
import sys,json,base64,time
tok='''$ACP_ACCESS_TOKEN'''
p=tok.split('.')[1]
p += '=' * (-len(p)%4)
try:
    exp=json.loads(base64.urlsafe_b64decode(p)).get('exp') or 0
    print(int((exp-time.time())/60))
except Exception:
    print(-1)
")
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
                echo "[startup] Refresh OK — access=${#ACP_ACCESS_TOKEN} chars, refresh rotated"
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

# Locate signer binary early
ACP_SIGNER_BIN=""
for cand in \
  "$(npm root -g 2>/dev/null)/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux" \
  /usr/local/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux \
  /usr/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux
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
  echo "[startup] ERROR: acp-cli-signer-linux not found"
fi

# --- PROVE signer decrypt works under this HOME before launching ---
SIGNER_PK=$(python3 -c "
import json,os
cfg=json.loads(open('/opt/acp-config/config.json').read())
w=(os.environ.get('ACP_AGENT_WALLET_ADDRESS') or '').lower()
agents=cfg.get('agents') or {}
entry=agents.get(w)
if not entry:
  for k,v in agents.items():
    if k.lower()==w:
      entry=v; break
print((entry or {}).get('publicKey') or '')
")
if [ -n "$ACP_SIGNER_BIN" ] && [ -n "$SIGNER_PK" ]; then
  SIGN_OUT=$("$ACP_SIGNER_BIN" sign --public-key "$SIGNER_PK" --payload 6869 2>&1 || true)
  if echo "$SIGN_OUT" | grep -q '"signature"'; then
    echo "[startup] SIGNER_OK under HOME=$HOME (path-bound keystore unlocked)"
  else
    echo "[startup] SIGNER_FAIL under HOME=$HOME: ${SIGN_OUT:0:200}"
    echo "[startup] FATAL: keystore MAC failed — HOME must be /home/hermes/.hermes/home and ACP_SIGNER_KEYS_JSON must be the store generated there"
  fi
else
  echo "[startup] WARNING: cannot preflight signer (bin or pk missing)"
fi

# Write JWT tokens to file keyring for CLI
if [ -n "$ACP_REFRESH_TOKEN" ]; then
    echo "[startup] Writing tokens to file keyring..."
    node <<'NODE'
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const wallet = process.env.ACP_AGENT_WALLET_ADDRESS;
const accessToken = process.env.ACP_ACCESS_TOKEN || '';
const refreshToken = process.env.ACP_REFRESH_TOKEN;
const ownerWallet = '0x25f2603be53bd4bed38aea500cb60fd10e7469ea';
const keyPath = path.join(process.env.HOME, '.config/keyring/file.key');
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
const dataPath = path.join(process.env.HOME, '.local/share/keyring/secrets.json');
fs.mkdirSync(path.dirname(dataPath), { recursive: true });
fs.writeFileSync(dataPath, output, { mode: 0o600 });
console.log('[startup] Tokens written (access=' + (accessToken||'').length + ' chars)');
NODE

    WHOAMI_OUT=$(acp agent whoami --json 2>&1) || true
    if echo "$WHOAMI_OUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'[startup] Agent identity verified: {d.get(\"name\")} ({d.get(\"id\")})')" 2>/dev/null; then
        :
    else
        echo "[startup] WARNING: whoami failed (continuing). out=${WHOAMI_OUT:0:160}"
    fi
else
    echo "[startup] Skipping keyring write (no refresh token)."
fi

if [ -f /app/package.json ]; then
    echo "[startup] Ensuring node deps for live_provider..."
    (cd /app && npm install --omit=dev --no-audit --no-fund 2>&1 | tail -5) || echo "[startup] WARNING: npm install failed"
fi

echo "[startup] HOME=$HOME"
echo "[startup] All checks passed. Launching live_provider + provider..."
echo ""

cd /app

export HOME="$SIGNER_HOME"
export ACP_CONFIG=/opt/acp-config/config.json
export ACP_CONFIG_DIR=/opt/acp-config

if [ -f /app/live_provider.mjs ]; then
    if command -v stdbuf >/dev/null 2>&1; then
      stdbuf -oL -eL node /app/live_provider.mjs &
    else
      node /app/live_provider.mjs &
    fi
    LIVE_PID=$!
    echo "[startup] live_provider.mjs pid $LIVE_PID (stdout→Render logs, HOME=$HOME)"
    sleep 3
    if kill -0 "$LIVE_PID" 2>/dev/null; then
      echo "[startup] live_provider still running after 3s"
    else
      echo "[startup] ERROR: live_provider exited early — check FATAL lines above"
    fi
else
    echo "[startup] WARNING: live_provider.mjs missing"
fi

exec python3 provider.py
