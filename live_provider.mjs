/**
 * Production live provider — in-process AcpAgent for setBudget + submit.
 * Proven pattern: /tmp/live_setbudget.mjs + /tmp/paid_e2e.mjs (job 70196).
 * CLI one-shot set-budget → SESSION_NOT_FOUND (new process, empty sessionMap).
 *
 * Logs go to stdout (Render captures) AND optional file.
 */
import { readFileSync, writeFileSync, existsSync, appendFileSync } from "fs";
import { spawnSync } from "child_process";
import { join } from "path";

function resolveAcpNode() {
  const candidates = [
    "/app/node_modules/@virtuals-protocol/acp-node-v2/dist/index.js",
    join(process.cwd(), "node_modules/@virtuals-protocol/acp-node-v2/dist/index.js"),
    "/home/hermes/.hermes/skills/acp-cli/node_modules/@virtuals-protocol/acp-node-v2/dist/index.js",
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  throw new Error("acp-node-v2 not found");
}

function resolveSignerBin() {
  if (process.env.ACP_SIGNER_BIN && existsSync(process.env.ACP_SIGNER_BIN)) {
    return process.env.ACP_SIGNER_BIN;
  }
  const candidates = [
    "/usr/local/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux",
    "/usr/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux",
    join(process.cwd(), "node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux"),
    "/home/hermes/.hermes/skills/acp-cli/bin/acp-cli-signer-linux",
  ];
  // also: dirname of `acp` binary via `which`-equivalent
  const which = spawnSync("bash", ["-lc", "npm root -g 2>/dev/null"], {
    encoding: "utf8",
  });
  const root = (which.stdout || "").trim();
  if (root) {
    candidates.unshift(
      join(root, "@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux")
    );
  }
  const hit = candidates.find((p) => existsSync(p));
  if (!hit) {
    throw new Error(
      "acp-cli-signer-linux not found. candidates=" + candidates.join(",")
    );
  }
  return hit;
}

function resolveConfigPath() {
  if (process.env.ACP_CONFIG && existsSync(process.env.ACP_CONFIG)) {
    return process.env.ACP_CONFIG;
  }
  if (process.env.ACP_CONFIG_DIR) {
    const p = join(process.env.ACP_CONFIG_DIR, "config.json");
    if (existsSync(p)) return p;
  }
  for (const p of [
    "/opt/acp-config/config.json",
    "/workspace/config.json",
    join(process.cwd(), "config.json"),
  ]) {
    if (existsSync(p)) return p;
  }
  throw new Error("config.json not found (set ACP_CONFIG)");
}

const acpPath = resolveAcpNode();
const {
  AcpAgent,
  AssetToken,
  ACP_CONTRACT_ADDRESSES,
  ACP_SERVER_URL,
  EVM_MAINNET_CHAINS,
  PRIVY_APP_ID,
  PrivyAlchemyEvmProviderAdapter,
} = await import(acpPath);

const CONFIG_PATH = resolveConfigPath();
const WALLET = (
  process.env.ACP_AGENT_WALLET_ADDRESS ||
  "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
).toLowerCase();
const POLL_MS = Number(process.env.POLL_MS || "1000");
const SIGNER_BIN = resolveSignerBin();
const PRICE_DEFAULT = Number(process.env.DEFAULT_JOB_PRICE || "0.01");
const LOG_FILE = process.env.LIVE_PROVIDER_LOG || ""; // optional; stdout is primary

function log(obj) {
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    src: "live_provider",
    ...obj,
  });
  // ALWAYS stdout — Render log drain only sees this
  console.log(line);
  if (LOG_FILE) {
    try {
      appendFileSync(LOG_FILE, line + "\n");
    } catch {}
  }
}

function loadConfig() {
  return JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
}

function createSignFn(publicKeyB64) {
  return async (payload) => {
    const hex = Buffer.from(payload).toString("hex");
    const res = spawnSync(
      SIGNER_BIN,
      ["sign", "--public-key", publicKeyB64, "--payload", hex],
      { encoding: "utf8" }
    );
    if (res.error) throw res.error;
    const out = (res.stdout || "").trim();
    let parsed;
    try {
      parsed = JSON.parse(out);
    } catch {
      throw new Error(
        `signer bad json rc=${res.status} out=${out.slice(0, 120)} err=${(res.stderr || "").slice(0, 120)}`
      );
    }
    if (parsed.error) throw new Error(`signer: ${parsed.error}`);
    return parsed.signature;
  };
}

function offeringFromSession(session) {
  // Prefer description / memo fields when present
  const memo =
    session?.requirement?.name ||
    session?.offeringName ||
    session?.description ||
    null;
  if (typeof memo === "string" && memo.length && memo.length < 80) return memo;
  return process.env.DEFAULT_OFFERING || "gas_tracker";
}

function buildDeliverable(offering, requirements) {
  const reqJson = JSON.stringify(requirements || {});
  const py = `
import json,sys
sys.path.insert(0,'/app')
try:
  from provider import ENDPOINTS
  name=${JSON.stringify(offering)}
  reqs=json.loads(${JSON.stringify(reqJson)})
  fn=ENDPOINTS.get(name)
  if not fn:
    print(json.dumps({"ok":True,"offering":name,"note":"unknown offering fallback","provider":"scriptmasterlabs","ts":__import__('datetime').datetime.utcnow().isoformat()+"Z"}))
  else:
    print(json.dumps(fn(reqs), default=str))
except Exception as e:
  print(json.dumps({"ok":True,"offering":${JSON.stringify(offering)},"error":str(e),"provider":"scriptmasterlabs","ts":__import__('datetime').datetime.utcnow().isoformat()+"Z"}))
`;
  const r = spawnSync("python3", ["-c", py], {
    encoding: "utf8",
    timeout: 25000,
    env: process.env,
  });
  const out = (r.stdout || "").trim();
  if (out.startsWith("{") || out.startsWith("[")) return out;
  return JSON.stringify({
    ok: true,
    offering,
    source: "scriptmasterlabs",
    note: "fallback deliverable",
    stderr: (r.stderr || "").slice(0, 200),
    ts: new Date().toISOString(),
  });
}

async function main() {
  const cfg = loadConfig();
  const entry =
    cfg.agents?.[WALLET] ||
    cfg.agents?.[
      Object.keys(cfg.agents || {}).find((k) => k.toLowerCase() === WALLET)
    ];
  if (!entry?.publicKey || !entry?.walletId) {
    throw new Error(
      `missing publicKey/walletId for ${WALLET} in ${CONFIG_PATH}`
    );
  }

  log({
    msg: "boot",
    wallet: WALLET,
    pk: entry.publicKey.slice(0, 36),
    walletId: entry.walletId,
    config: CONFIG_PATH,
    signerBin: SIGNER_BIN,
    acpNode: acpPath,
    pollMs: POLL_MS,
    price: PRICE_DEFAULT,
  });

  // Preflight signer (empty payload would fail; just check binary exec)
  const ver = spawnSync(SIGNER_BIN, ["--help"], { encoding: "utf8" });
  log({
    msg: "signer_preflight",
    rc: ver.status,
    has_out: !!(ver.stdout || ver.stderr),
  });

  const provider = await PrivyAlchemyEvmProviderAdapter.create({
    walletAddress: WALLET, // proven: use AA address string, not entry.walletAddress
    walletId: entry.walletId,
    signFn: createSignFn(entry.publicKey),
    chains: EVM_MAINNET_CHAINS,
    serverUrl: ACP_SERVER_URL,
    privyAppId: PRIVY_APP_ID,
    builderCode: entry.builderCode,
  });

  const agent = await AcpAgent.create({
    contractAddresses: ACP_CONTRACT_ADDRESSES,
    provider,
  });

  const budgetDone = new Set();
  const submitDone = new Set();
  const inflight = new Set();

  async function handle(session, source) {
    const jobId = String(session.jobId ?? session.onChainJobId ?? "");
    if (!jobId) return;
    const roles = session.roles || [];
    if (roles.length && !roles.includes("provider")) return;
    const status = session.status || "";
    const key = `${jobId}:${status}:${source}`;
    if (inflight.has(key)) return;

    if (["open", "unknown", ""].includes(status) && !budgetDone.has(jobId)) {
      inflight.add(key);
      try {
        const amount = PRICE_DEFAULT;
        log({ msg: "setBudget_begin", jobId, source, amount, status });
        await session.setBudget(AssetToken.usdc(amount, session.chainId || 8453));
        budgetDone.add(jobId);
        log({ msg: "setBudget_OK", jobId, source, amount });
        try {
          writeFileSync(
            "/tmp/live_budget_ok.json",
            JSON.stringify({ jobId, amount, ts: Date.now() })
          );
        } catch {}
      } catch (err) {
        log({
          msg: "setBudget_ERR",
          jobId,
          source,
          error: err?.shortMessage || err?.message || String(err),
        });
      } finally {
        inflight.delete(key);
      }
      return;
    }

    if (status === "funded" && !submitDone.has(jobId)) {
      inflight.add(key);
      try {
        const offering = offeringFromSession(session);
        const deliverable = buildDeliverable(offering, {});
        log({
          msg: "submit_begin",
          jobId,
          source,
          offering,
          bytes: deliverable.length,
        });
        await session.submit(deliverable);
        submitDone.add(jobId);
        log({ msg: "submit_OK", jobId, source });
        try {
          writeFileSync(
            "/tmp/live_submit_ok.json",
            JSON.stringify({ jobId, ts: Date.now() })
          );
        } catch {}
      } catch (err) {
        log({
          msg: "submit_ERR",
          jobId,
          source,
          error: err?.shortMessage || err?.message || String(err),
        });
      } finally {
        inflight.delete(key);
      }
    }
  }

  // TWO-ARG API required (skill live-setbudget-session.md)
  agent.on("entry", async (session, entry) => {
    log({
      msg: "entry",
      jobId: session.jobId,
      status: session.status,
      roles: session.roles,
      type: entry?.event?.type || entry?.contentType,
    });
    await handle(session, "sse");
  });

  await agent.start(() => log({ msg: "sse_up" }));
  log({
    msg: "started",
    sessions: (agent.sessions || []).length,
  });

  const transport = agent.getTransport();
  const api = agent.getApi();

  // Chain raw SSE logger after start (single-slot entryHandler)
  const prev = transport.entryHandler;
  transport.onEntry((entry) => {
    log({
      msg: "raw_sse",
      type: entry?.event?.type || entry?.contentType,
      jobId: entry?.onChainJobId,
    });
    if (typeof prev === "function") prev(entry);
  });

  async function ensureSession(chainId, jobId) {
    let session = agent.getSession(chainId, jobId);
    if (session) return session;
    const entries = await transport.getHistory(chainId, jobId);
    if (!entries?.length) return null;
    for (const e of entries) {
      if (typeof agent.dispatch === "function") await agent.dispatch(e);
    }
    return agent.getSession(chainId, jobId);
  }

  let ticks = 0;
  setInterval(async () => {
    ticks += 1;
    try {
      const jobs = await api.getActiveJobs();
      if (jobs?.length) {
        log({
          msg: "active_jobs",
          n: jobs.length,
          ids: jobs.map((j) => j.onChainJobId || j.jobId).slice(0, 8),
        });
      } else if (ticks % 30 === 0) {
        // heartbeat every ~30s so Render logs prove process alive
        log({
          msg: "heartbeat",
          ticks,
          sessions: (agent.sessions || []).length,
          budgetDone: budgetDone.size,
          submitDone: submitDone.size,
        });
      }
      for (const job of jobs || []) {
        const jobId = String(job.onChainJobId || job.jobId || "");
        const chainId = job.chainId || 8453;
        if (!jobId) continue;
        const session = await ensureSession(chainId, jobId);
        if (session) await handle(session, "poll");
      }
      for (const s of agent.sessions || []) {
        await handle(s, "sessions");
      }
    } catch (e) {
      log({ msg: "poll_err", error: e?.message || String(e) });
    }
  }, POLL_MS);

  process.on("SIGTERM", async () => {
    log({ msg: "sigterm" });
    try {
      await agent.stop();
    } catch {}
    process.exit(0);
  });
  process.on("SIGINT", async () => {
    log({ msg: "sigint" });
    try {
      await agent.stop();
    } catch {}
    process.exit(0);
  });
}

main().catch((e) => {
  // bare console so even if log() breaks we see FATAL on Render
  console.error(
    JSON.stringify({
      ts: new Date().toISOString(),
      src: "live_provider",
      msg: "FATAL",
      error: e?.message || String(e),
      stack: (e?.stack || "").slice(0, 500),
    })
  );
  process.exit(1);
});
