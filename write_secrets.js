#!/usr/bin/env node
/**
 * Write ACP tokens to the cross-keyring file backend.
 * Forces file backend to avoid native Linux keyring "invalid characters" error.
 * Usage: node write_secrets.js <wallet> <accessToken> <refreshToken>
 */
const path = require('path');
const keyringPath = path.join(__dirname, 'node_modules', '@virtuals-protocol', 'acp-cli', 'node_modules', 'cross-keychain', 'dist', 'index.js');

async function main() {
    const [,, wallet, accessToken, refreshToken] = process.argv;
    if (!wallet || !accessToken || !refreshToken) {
        console.error('Usage: node write_secrets.js <wallet> <accessToken> <refreshToken>');
        process.exit(1);
    }
    
    const { setPassword, getPassword, useBackend } = require(keyringPath);
    
    // Force file backend (bypasses native Linux keyring which rejects wallet addresses)
    await useBackend('file');
    
    const service = 'acp-auth';
    const w = wallet.toLowerCase();
    
    await setPassword(service, `access-token-${w}`, accessToken);
    await setPassword(service, `refresh-token-${w}`, refreshToken);
    
    // Verify
    const at = await getPassword(service, `access-token-${w}`);
    const rt = await getPassword(service, `refresh-token-${w}`);
    
    if (at && rt) {
        console.log(`[write_secrets] Tokens written to file keyring (access=${at.length} chars, refresh=${rt.length} chars)`);
    } else {
        console.error('[write_secrets] Failed to verify tokens');
        process.exit(1);
    }
}

main().catch(e => { console.error('[write_secrets] Error:', e.message); process.exit(1); });
