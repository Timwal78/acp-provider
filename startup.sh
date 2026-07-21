#!/usr/bin/env bash
# ============================================================
# Render worker — MONEY PATH ONLY
#
# Needs:
#   ACP_CONFIG_JSON, ACP_SIGNER_KEYS_JSON, ACP_AGENT_WALLET_ADDRESS
#   ACP_KEYRING_KEY_B64 (optional leftover; not required for intake)
#
# Does NOT need ACP_ACCESS_TOKEN / ACP_REFRESH_TOKEN for:
#   - job discovery (public REST)
#   - setBudget/submit (signer via live_provider)
#
# Signer keystore is HOME-path-bound:
#   HOME=/home/hermes/.hermes/home
# ============================================================
set -e

echo "[startup] ACP Provider — money path boot"

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
unset XDG_CONFIG_HOME || true
export XDG_DATA_HOME="$HOME/.local/share"
export TS_KEYRING_BACKEND=file

# --- config.json ---
if [ -z "$ACP_CONFIG_JSON" ]; then
  echo "[startup] ERROR: ACP_CONFIG_JSON missing"; exit 1
fi
printf '%s' "$ACP_CONFIG_JSON" > /opt/acp-config/config.json
cp -f /opt/acp-config/config.json "$HOME/.config/acp/config.json"
echo "[startup] Wrote config.json"

# --- signer-keys (REQUIRED for setBudget) ---
if [ -z "$ACP_SIGNER_KEYS_JSON" ]; then
  echo "[startup] ERROR: ACP_SIGNER_KEYS_JSON missing"; exit 1
fi
printf '%s' "$ACP_SIGNER_KEYS_JSON" > "$HOME/.config/acp-cli/signer-keys.json"
cp -f "$HOME/.config/acp-cli/signer-keys.json" /opt/acp-config/acp-cli/signer-keys.json
chmod 600 "$HOME/.config/acp-cli/signer-keys.json"
echo "[startup] Wrote signer-keys.json (HOME-path-bound)"

# --- optional keyring key (CLI only; intake does not need JWT) ---
if [ -n "${ACP_KEYRING_KEY_B64:-}" ]; then
  echo "$ACP_KEYRING_KEY_B64" | base64 -d > "$HOME/.config/keyring/file.key" 2>/dev/null || true
  cp -f "$HOME/.config/keyring/file.key" /opt/acp-config/keyring/file.key 2>/dev/null || true
  chmod 600 "$HOME/.config/keyring/file.key" 2>/dev/null || true
fi

ACP_AGENT_WALLET_ADDRESS=$(echo -n "${ACP_AGENT_WALLET_ADDRESS:-}" | tr -d '[:space:]')
if [ -z "$ACP_AGENT_WALLET_ADDRESS" ]; then
  echo "[startup] ERROR: ACP_AGENT_WALLET_ADDRESS missing"; exit 1
fi
export ACP_AGENT_WALLET_ADDRESS

# Tokens are OPTIONAL. Never refresh. Never exit on missing JWT.
# (Refresh rotation was burning tokens and killing intake for no reason.)
export ACP_ACCESS_TOKEN="${ACP_ACCESS_TOKEN:-}"
export ACP_REFRESH_TOKEN="${ACP_REFRESH_TOKEN:-}"
echo "[startup] JWT optional (intake=public REST). refresh_set=$([ -n "$ACP_REFRESH_TOKEN" ] && echo yes || echo no)"

# ACP CLI optional (for rare admin); do not fail boot
if ! command -v acp >/dev/null 2>&1; then
  echo "[startup] Installing ACP CLI..."
  npm i -g @virtuals-protocol/acp-cli >/dev/null 2>&1 || echo "[startup] WARN: acp-cli install failed"
fi
acp --version 2>/dev/null || true

# Signer binary
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
if [ -z "$ACP_SIGNER_BIN" ]; then
  echo "[startup] ERROR: acp-cli-signer-linux not found"; exit 1
fi
export ACP_SIGNER_BIN
echo "[startup] ACP_SIGNER_BIN=$ACP_SIGNER_BIN"

# Preflight signer decrypt under pinned HOME
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
if [ -n "$SIGNER_PK" ]; then
  SIGN_OUT=$("$ACP_SIGNER_BIN" sign --public-key "$SIGNER_PK" --payload 6869 2>&1 || true)
  if echo "$SIGN_OUT" | grep -q '"signature"'; then
    echo "[startup] SIGNER_OK under HOME=$HOME"
  else
    echo "[startup] FATAL SIGNER_FAIL: ${SIGN_OUT:0:200}"
    exit 1
  fi
else
  echo "[startup] FATAL: no publicKey in config"; exit 1
fi

if [ -f /app/package.json ]; then
  echo "[startup] Ensuring node deps..."
  (cd /app && npm install --omit=dev --no-audit --no-fund 2>&1 | tail -3) || echo "[startup] WARN: npm install failed"
fi

echo "[startup] Launching live_provider + provider (no JWT gate)"
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
  echo "[startup] live_provider.mjs pid $LIVE_PID"
  sleep 3
  if kill -0 "$LIVE_PID" 2>/dev/null; then
    echo "[startup] live_provider running"
  else
    echo "[startup] ERROR: live_provider died — check FATAL above"
  fi
else
  echo "[startup] ERROR: live_provider.mjs missing"; exit 1
fi

exec python3 provider.py
