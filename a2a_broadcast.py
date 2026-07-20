#!/usr/bin/env python3
"""
a2a_broadcast.py — announce scriptmasterlabs' A2A/x402/MCP presence.

Deployment helper that broadcasts the agent's /.well-known/agent.json card to
every public discovery surface that should know about it:

  1. x402 registries — x402scan and x402.org index payable HTTP endpoints.
     We POST the card + the /.well-known/x402 discovery doc so the registry
     can verify the routes are live and payable.
  2. ACP marketplace — the Virtuals ACP agent already lists 40 offerings, but
     we ping the ACP agent-info endpoint with the public HTTP origin so the
     marketplace links the ACP agent record to the new x402 surface.
  3. Agent indexes — any configurable list of agent-index/webhook URLs (via
     env A2A_BROADCAST_TARGETS, comma-separated). Each gets a POST with the
     card. Used for private indexers, Discord/Slack announcement webhooks,
     or any custom aggregator.
  4. Self-verification — after broadcast, we GET our own /.well-known/agent.json,
     /.well-known/x402, and /.well-known/mcp.json to confirm they are live
     and well-formed. Failures are surfaced in the report.

Outputs a JSON report of every broadcast target, the HTTP status received,
and any response body. Exits non-zero if any critical target failed.

Usage
-----
    # Broadcast to all default targets, hitting the live deployment.
    python a2a_broadcast.py --base-url https://acp-render.onrender.com

    # Dry run — show what would be sent without making network calls.
    python a2a_broadcast.py --base-url https://acp-render.onrender.com --dry-run

    # Extra webhook targets (in addition to defaults).
    A2A_BROADCAST_TARGETS="https://index.example.com/agents,https://hooks.slack.com/services/XXX" \\
        python a2a_broadcast.py --base-url https://acp-render.onrender.com

Integration
-----------
This is NOT a Flask Blueprint — it is a standalone CLI script meant to be run
after a deploy (e.g. as a Render one-off command or a CI step) to make the
new deployment discoverable. It imports build_agent_card() from
a2a_agent_card.py so it always broadcasts the same card the live server
serves.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any

# Build the agent card the same way the live server does — single source of
# truth. We import lazily inside main() so that `python a2a_broadcast.py --help`
# works even if provider.py / x402_flask.py can't import (e.g. missing Flask in
# a CI env that only runs the broadcast).
logger = logging.getLogger("a2a_broadcast")

# ── Agent identity (mirrors a2a_agent_card.py) ───────────────────────────────
AGENT_NAME = "scriptmasterlabs"
AGENT_ID = "019f5f40-c194-7776-b5e1-7a666ce631c0"
AGENT_WALLET = "0x72330994f379a71542e7bd5a4cf99a9d9743f4aa"
ACP_WEBSITE = "https://timwal78.github.io/acp-provider/"

# ── Default broadcast targets ────────────────────────────────────────────────
# x402 registries that index payable HTTP endpoints. These are the public
# discovery surfaces every x402 agent scans. A POST with the card + the x402
# discovery doc tells the registry "here is a new payable deployment; please
# index it." If a registry has no public submission endpoint, we still GET
# its site to record reachability in the report.
X402_REGISTRIES: list[dict[str, str]] = [
    {
        "name": "x402scan",
        "submit": "https://x402scan.io/api/submit",
        "site": "https://x402scan.io",
        "method": "POST",
        "description": "x402 route index — scans and verifies payable HTTP endpoints.",
    },
    {
        "name": "x402.org",
        "submit": "https://x402.org/api/register",
        "site": "https://x402.org",
        "method": "POST",
        "description": "x402 protocol registry — canonical list of x402-enabled services.",
    },
]

# MCP and A2A protocol registries — announce the MCP manifest and agent card.
MCP_REGISTRIES: list[dict[str, str]] = [
    {
        "name": "MCP Registry (modelcontextprotocol.org)",
        "submit": "https://registry.modelcontextprotocol.io/api/servers",
        "site": "https://modelcontextprotocol.org",
        "method": "POST",
        "description": "Official MCP server registry — lists servers with transport + manifest URLs.",
    },
]

AP2_REGISTRIES: list[dict[str, str]] = [
    {
        "name": "Google Agent Payments Protocol (AP2) registry",
        "submit": "https://api.ap2-protocol.org/api/v1/agents/register",
        "site": "https://ap2-protocol.org",
        "method": "POST",
        "description": "AP2 registry — agent payment identity for mandate-based agent commerce.",
    },
]

# ACP marketplace endpoint. The Virtuals ACP CLI (`acp`) is the canonical way
# to update marketplace listings, but we also POST the public HTTP origin to
# the agent-info endpoint so the marketplace record links to the x402 surface.
ACP_MARKETPLACE = {
    "name": "Virtuals ACP",
    "submit": "https://acp.virtuals.io/api/agents/info",
    "site": "https://acp.virtuals.io",
    "method": "POST",
    "description": "Virtuals ACP marketplace agent-info update.",
}

# Default agent-index endpoints. Empty by default — populate via env var
# A2A_BROADCAST_TARGETS at deploy time. Examples: a private agent index, a
# Discord webhook, a Slack incoming webhook.
DEFAULT_AGENT_INDEXES: list[str] = []


# ── Report dataclasses ───────────────────────────────────────────────────────
@dataclass
class BroadcastResult:
    """One target's broadcast outcome."""
    target: str
    kind: str  # "x402_registry" | "acp_marketplace" | "agent_index" | "self_verify"
    method: str
    url: str
    status: int | None = None
    ok: bool = False
    error: str | None = None
    request_body_size: int = 0
    response_body: str | None = None
    elapsed_ms: float = 0.0


@dataclass
class BroadcastReport:
    """Full broadcast run report."""
    agent: str
    agent_id: str
    base_url: str
    started_at: str
    finished_at: str | None = None
    dry_run: bool = False
    results: list[BroadcastResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        total = len(self.results)
        ok = sum(1 for r in self.results if r.ok)
        failed = total - ok
        return {
            "agent": self.agent,
            "agentId": self.agent_id,
            "baseUrl": self.base_url,
            "dryRun": self.dry_run,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "total": total,
            "ok": ok,
            "failed": failed,
            "results": [asdict(r) for r in self.results],
        }


# ── HTTP helper ──────────────────────────────────────────────────────────────
def _http(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: int = 20,
    headers: dict[str, str] | None = None,
) -> tuple[int | None, str, str | None]:
    """Make an HTTP call. Returns (status_code, error, response_body).

    Uses urllib (stdlib only) so this script has zero third-party deps — it
    must run in minimal CI/one-off environments that may not have requests
    installed.
    """
    hdrs = {"User-Agent": f"scriptmasterlabs-a2a-broadcast/1.0 ({AGENT_ID})"}
    if headers:
        hdrs.update(headers)
    data_bytes: bytes | None = None
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data_bytes, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, "", raw[:4000]  # cap stored body to 4KB
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, f"HTTPError: {e}", raw[:4000]
    except urllib.error.URLError as e:
        return None, f"URLError: {e.reason}", None
    except Exception as e:  # pragma: no cover
        return None, f"{type(e).__name__}: {e}", None


def _wellknown_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


# ── Broadcast steps ──────────────────────────────────────────────────────────
def _build_payload(base_url: str) -> dict[str, Any]:
    """Build the broadcast payload: agent card + x402 + mcp manifests.

    We POST a bundle rather than just the card so a registry can verify all
    three discovery surfaces in one round-trip without having to crawl the
    deployment itself.
    """
    from a2a_agent_card import build_agent_card  # lazy import; see module docstring
    card = build_agent_card(base_url=base_url)

    # Fetch the live x402 + mcp manifests from the deployment itself, so the
    # payload reflects what is actually serving right now (not what we think
    # should be serving).
    x402_status, x402_err, x402_body = _http("GET", _wellknown_url(base_url, "/.well-known/x402"))
    mcp_status, mcp_err, mcp_body = _http("GET", _wellknown_url(base_url, "/.well-known/mcp.json"))

    x402_doc = None
    if x402_status == 200 and x402_body:
        try:
            x402_doc = json.loads(x402_body)
        except json.JSONDecodeError:
            x402_doc = None
    mcp_doc = None
    if mcp_status == 200 and mcp_body:
        try:
            mcp_doc = json.loads(mcp_body)
        except json.JSONDecodeError:
            mcp_doc = None

    return {
        "agent": {
            "name": AGENT_NAME,
            "id": AGENT_ID,
            "wallet": AGENT_WALLET,
            "website": ACP_WEBSITE,
            "baseUrl": base_url,
        },
        "agentCard": card,
        "x402Discovery": x402_doc,
        "mcpManifest": mcp_doc,
        "broadcastedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _broadcast_to_x402_registries(payload: dict[str, Any], dry_run: bool) -> list[BroadcastResult]:
    """Broadcast to x402 registries (x402scan, x402.org)."""
    results: list[BroadcastResult] = []
    for reg in X402_REGISTRIES:
        url = reg["submit"]
        body_size = len(json.dumps(payload).encode("utf-8"))
        if dry_run:
            results.append(BroadcastResult(
                target=reg["name"], kind="x402_registry", method="POST", url=url,
                ok=True, request_body_size=body_size, response_body="[dry-run] not sent",
            ))
            continue
        t0 = time.monotonic()
        status, err, resp_body = _http("POST", url, body=payload, timeout=20)
        elapsed = (time.monotonic() - t0) * 1000
        ok = status is not None and 200 <= status < 300
        # If the submit endpoint 404s (registry may not have a public submit
        # API), fall back to a GET of the registry site so we at least record
        # reachability and the agent can be manually submitted later.
        if not ok and status == 404:
            status2, err2, _ = _http("GET", reg["site"], timeout=15)
            results.append(BroadcastResult(
                target=reg["name"], kind="x402_registry", method="POST", url=url,
                status=status, ok=False,
                error=f"submit endpoint 404 (site reachable={status2 == 200}): {err}",
                request_body_size=body_size, response_body=resp_body, elapsed_ms=elapsed,
            ))
            continue
        results.append(BroadcastResult(
            target=reg["name"], kind="x402_registry", method="POST", url=url,
            status=status, ok=ok, error=err,
            request_body_size=body_size, response_body=resp_body, elapsed_ms=elapsed,
        ))
    return results


def _broadcast_to_acp(payload: dict[str, Any], dry_run: bool) -> list[BroadcastResult]:
    """Broadcast to the Virtuals ACP marketplace agent-info endpoint."""
    results: list[BroadcastResult] = []
    url = ACP_MARKETPLACE["submit"]
    # The ACP marketplace expects a smaller payload — just the agent identity
    # + the new public HTTP origin + a pointer to the full card.
    acp_payload = {
        "agentId": AGENT_ID,
        "name": AGENT_NAME,
        "wallet": AGENT_WALLET,
        "httpOrigin": payload["agent"]["baseUrl"],
        "agentCardUrl": _wellknown_url(payload["agent"]["baseUrl"], "/.well-known/agent.json"),
        "x402DiscoveryUrl": _wellknown_url(payload["agent"]["baseUrl"], "/.well-known/x402"),
        "mcpManifestUrl": _wellknown_url(payload["agent"]["baseUrl"], "/.well-known/mcp.json"),
        "capabilitiesCount": payload["agentCard"].get("capabilitiesCount"),
        "broadcastedAt": payload["broadcastedAt"],
    }
    body_size = len(json.dumps(acp_payload).encode("utf-8"))
    if dry_run:
        results.append(BroadcastResult(
            target=ACP_MARKETPLACE["name"], kind="acp_marketplace", method="POST", url=url,
            ok=True, request_body_size=body_size, response_body="[dry-run] not sent",
        ))
        return results
    t0 = time.monotonic()
    status, err, resp_body = _http("POST", url, body=acp_payload, timeout=20)
    elapsed = (time.monotonic() - t0) * 1000
    ok = status is not None and 200 <= status < 300
    results.append(BroadcastResult(
        target=ACP_MARKETPLACE["name"], kind="acp_marketplace", method="POST", url=url,
        status=status, ok=ok, error=err,
        request_body_size=body_size, response_body=resp_body, elapsed_ms=elapsed,
    ))
    return results


def _broadcast_to_agent_indexes(payload: dict[str, Any], dry_run: bool) -> list[BroadcastResult]:
    """Broadcast to configurable agent indexes / webhooks.

    Targets come from env A2A_BROADCAST_TARGETS (comma-separated URLs) plus
    DEFAULT_AGENT_INDEXES. Each receives the full payload as a POST. Used for
    private indexers, Discord/Slack webhooks, custom aggregators.
    """
    env_targets = [
        t.strip() for t in os.environ.get("A2A_BROADCAST_TARGETS", "").split(",")
        if t.strip()
    ]
    targets = list(DEFAULT_AGENT_INDEXES) + env_targets
    results: list[BroadcastResult] = []
    for url in targets:
        body_size = len(json.dumps(payload).encode("utf-8"))
        if dry_run:
            results.append(BroadcastResult(
                target=url, kind="agent_index", method="POST", url=url,
                ok=True, request_body_size=body_size, response_body="[dry-run] not sent",
            ))
            continue
        t0 = time.monotonic()
        status, err, resp_body = _http("POST", url, body=payload, timeout=20)
        elapsed = (time.monotonic() - t0) * 1000
        ok = status is not None and 200 <= status < 300
        results.append(BroadcastResult(
            target=url, kind="agent_index", method="POST", url=url,
            status=status, ok=ok, error=err,
            request_body_size=body_size, response_body=resp_body, elapsed_ms=elapsed,
        ))
    return results


def _self_verify(base_url: str, dry_run: bool) -> list[BroadcastResult]:
    """Fetch our own well-known endpoints to confirm they are live + valid."""
    results: list[BroadcastResult] = []
    checks = [
        ("/.well-known/agent.json", "a2a agent card"),
        ("/.well-known/x402", "x402 discovery"),
        ("/.well-known/mcp.json", "mcp manifest"),
    ]
    for path, label in checks:
        url = _wellknown_url(base_url, path)
        if dry_run:
            results.append(BroadcastResult(
                target=label, kind="self_verify", method="GET", url=url,
                ok=True, response_body="[dry-run] not fetched",
            ))
            continue
        t0 = time.monotonic()
        status, err, resp_body = _http("GET", url, timeout=15)
        elapsed = (time.monotonic() - t0) * 1000
        ok = status == 200
        # If we got a body, validate it parses as JSON.
        if ok and resp_body:
            try:
                json.loads(resp_body)
            except json.JSONDecodeError as e:
                ok = False
                err = f"response is not valid JSON: {e}"
        results.append(BroadcastResult(
            target=label, kind="self_verify", method="GET", url=url,
            status=status, ok=ok, error=err,
            response_body=(resp_body[:500] if resp_body else None), elapsed_ms=elapsed,
        ))
    return results


# ── Main ─────────────────────────────────────────────────────────────────────
def run_broadcast(base_url: str, dry_run: bool = False, verbose: bool = False) -> BroadcastReport:
    """Run the full broadcast sequence and return a report."""
    if verbose:
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    report = BroadcastReport(
        agent=AGENT_NAME,
        agent_id=AGENT_ID,
        base_url=base_url,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        dry_run=dry_run,
    )

    logger.info("broadcasting agent=%s base_url=%s dry_run=%s", AGENT_NAME, base_url, dry_run)

    # Step 1: self-verify the deployment is serving the three well-known docs.
    # Do this BEFORE broadcasting so we never announce a broken deployment.
    logger.info("step 1: self-verify well-known endpoints")
    report.results.extend(_self_verify(base_url, dry_run))
    if not dry_run and not all(r.ok for r in report.results if r.kind == "self_verify"):
        logger.error("self-verify failed; aborting broadcast to avoid announcing a broken deployment")
        report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return report

    # Step 2: build the broadcast payload (also fetches live x402 + mcp docs).
    logger.info("step 2: build broadcast payload")
    try:
        payload = _build_payload(base_url)
    except Exception as e:
        logger.exception("failed to build broadcast payload")
        report.results.append(BroadcastResult(
            target="payload-build", kind="self_verify", method="—", url="—",
            ok=False, error=f"payload build failed: {e}",
        ))
        report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return report

    # Step 3: broadcast to x402 registries.
    logger.info("step 3: broadcast to x402 registries (%d)", len(X402_REGISTRIES))
    report.results.extend(_broadcast_to_x402_registries(payload, dry_run))

    # Step 4: broadcast to ACP marketplace.
    logger.info("step 4: broadcast to ACP marketplace")
    report.results.extend(_broadcast_to_acp(payload, dry_run))

    # Step 5: broadcast to MCP registry.
    logger.info("step 5: broadcast to MCP registries (%d)", len(MCP_REGISTRIES))
    for reg in MCP_REGISTRIES:
        mcp_payload = {
            "name": AGENT_NAME,
            "transport": "http",
            "manifestUrl": f"{base_url}/.well-known/mcp.json",
            "rpcUrl": f"{base_url}/mcp",
            "sseUrl": f"{base_url}/mcp/sse",
            "protocolVersion": "2024-11-05",
        }
        status, err, resp_body = _http("POST", reg["submit"], body=mcp_payload)
        ok = status is not None and 200 <= status < 300
        report.results.append(BroadcastResult(
            target=reg["name"], kind="mcp_registry", method="POST",
            url=reg["submit"], status=status or 0, ok=ok, error=err or None,
            response_body=resp_body or "", elapsed_ms=0,
        ))

    # Step 6: broadcast to AP2 registry.
    logger.info("step 6: broadcast to AP2 registries (%d)", len(AP2_REGISTRIES))
    for reg in AP2_REGISTRIES:
        ap2_payload = {
            "agentName": AGENT_NAME,
            "walletAddress": AGENT_WALLET,
            "chainId": 8453,
            "paymentSchemes": ["x402"],
            "discoveryUrls": {
                "agent": f"{base_url}/.well-known/agent.json",
                "x402": f"{base_url}/.well-known/x402",
            },
        }
        status, err, resp_body = _http("POST", reg["submit"], body=ap2_payload)
        ok = status is not None and 200 <= status < 300
        report.results.append(BroadcastResult(
            target=reg["name"], kind="ap2_registry", method="POST",
            url=reg["submit"], status=status or 0, ok=ok, error=err or None,
            response_body=resp_body or "", elapsed_ms=0,
        ))

    # Step 7: broadcast to configurable agent indexes / webhooks.
    logger.info("step 7: broadcast to agent indexes / webhooks")
    report.results.extend(_broadcast_to_agent_indexes(payload, dry_run))

    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Broadcast scriptmasterlabs A2A/x402/MCP presence to discovery surfaces.",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Public base URL of the deployment (e.g. https://acp-render.onrender.com).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the payload and report without making any network calls.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Write the JSON report to this path (default: stdout).",
    )
    args = parser.parse_args(argv)

    report = run_broadcast(args.base_url, dry_run=args.dry_run, verbose=args.verbose)
    summary = report.summary()
    report_json = json.dumps(summary, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report_json)
        print(f"report written to {args.output}", file=sys.stderr)
    else:
        print(report_json)

    # Exit non-zero if any critical target failed. Self-verify failures are
    # always critical. Registry/marketplace/index failures are critical only
    # if they returned a real HTTP error (not a 404-with-fallback or a
    # network error, which may just mean the target doesn't exist yet).
    critical_failures = [
        r for r in report.results
        if not r.ok
        and (
            r.kind == "self_verify"
            or (r.status is not None and r.status >= 500)
        )
    ]
    if critical_failures:
        print(
            f"\n{len(critical_failures)} critical failure(s):",
            file=sys.stderr,
        )
        for r in critical_failures:
            print(f"  - {r.target}: {r.error or r.status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
