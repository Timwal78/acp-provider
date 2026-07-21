/**
 * Production live provider — in-process AcpAgent for setBudget + submit.
 * CLI one-shot provider set-budget/submit always SESSION_NOT_FOUND (new process).
 * REST poll in provider.py discovers; this process signs.
 */
import { readFileSync, writeFileSync, existsSync, appendFileSync } from "fs";
import { spawnSync } from "child_process";
import { createRequire } from "module";
import { homedir } from "os";
import { join } from "path";

const require = createRequire(import.meta.url);

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

const CONFIG_PATH =
  process.env.ACP_CONFIG ||
  process.env.ACP_CONFIG_DIR + "/config.json" ||
  "/opt/acp-config/config.json";
const WALLET = (
  process.env.ACP_AGENT_WALLET_ADDRESS ||
  "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
).toLowerCase();
const POLL_MS = Number(process.env.POLL_MS || "1000");
const SIGNER_BIN =
  process.env.ACP_SIGNER_BIN ||
  [
    "/usr/local/lib/node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux",
    "/home/hermes/.hermes/skills/acp-cli/bin/acp-cli-signer-linux",
    join(process.cwd(), "node_modules/@virtuals-protocol/acp-cli/bin/acp-cli-signer-linux"),
  ].find((p) => existsSync(p)) ||
  "acp-cli-signer-linux";

const PRICE_DEFAULT = Number(process.env.DEFAULT_JOB_PRICE || "0.01");
const LOG = process.env.LIVE_PROVIDER_LOG || "/app/live_provider.log";

function log(obj) {
  const line = JSON.stringify({ ts: new Date().toISOString(), ...obj });
  console.log(line);
  try {
    appendFileSync(LOG, line + "\n");
  } catch {}
}

function loadConfig() {
  const raw = readFileSync(CONFIG_PATH, "utf8");
  return JSON.parse(raw);
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
    const parsed = JSON.parse(out);
    if (parsed.error) throw new Error(`signer: ${parsed.error}`);
    return parsed.signature;
  };
}

function offeringFromSession(session) {
  // Try requirement / history later; default gas_tracker wedge
  return process.env.DEFAULT_OFFERING || "gas_tracker";
}

function buildDeliverable(offering, requirements) {
  // Prefer Python ENDPOINTS from provider.py
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
    print(json.dumps({"ok":True,"offering":name,"note":"unknown offering fallback","ts":__import__('datetime').datetime.utcnow().isoformat()+"Z"}))
  else:
    print(json.dumps(fn(reqs), default=str))
except Exception as e:
  print(json.dumps({"ok":True,"offering":${JSON.stringify(offering)},"error":str(e),"ts":__import__('datetime').datetime.utcnow().isoformat()+"Z"}))
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
    cfg.agents?.[Object.keys(cfg.agents || {}).find((k) => k.toLowerCase() === WALLET)];
  if (!entry?.publicKey || !entry?.walletId) {
    throw new Error(`missing publicKey/walletId for ${WALLET} in ${CONFIG_PATH}`);
  }
  log({
    msg: "boot",
    wallet: WALLET,
    pk: entry.publicKey.slice(0, 36),
    config: CONFIG_PATH,
    signerBin: SIGNER_BIN,
    pollMs: POLL_MS,
  });

  const provider = await PrivyAlchemyEvmProviderAdapter.create({
    walletAddress: entry.walletAddress || WALLET,
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
        writeFileSync(
          "/tmp/live_budget_ok.json",
          JSON.stringify({ jobId, amount, ts: Date.now() })
        );
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
        writeFileSync(
          "/tmp/live_submit_ok.json",
          JSON.stringify({ jobId, ts: Date.now() })
        );
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

  agent.on("entry", async (session, entry) => {
    log({
      msg: "entry",
      jobId: session.jobId,
      status: session.status,
      type: entry?.event?.type || entry?.contentType,
    });
    await handle(session, "sse");
  });

  await agent.start(() => log({ msg: "sse_up" }));
  log({ msg: "started" });

  const transport = agent.getTransport();
  const api = agent.getApi();

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

  setInterval(async () => {
    try {
      const jobs = await api.getActiveJobs();
      if (jobs?.length) {
        log({
          msg: "active_jobs",
          n: jobs.length,
          ids: jobs.map((j) => j.onChainJobId || j.jobId).slice(0, 8),
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
    try {
      await agent.stop();
    } catch {}
    process.exit(0);
  });
  process.on("SIGINT", async () => {
    try {
      await agent.stop();
    } catch {}
    process.exit(0);
  });
}

main().catch((e) => {
  console.error("FATAL", e?.message || e);
  process.exit(1);
});
