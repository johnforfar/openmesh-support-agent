#!/usr/bin/env python3
"""
End-to-end verification suite for a deployed Openmesh Support Agent.

Run this against a live deployment after `om app expose ...`. It checks:

  1. HTTPS reachable + valid TLS
  2. Basic security posture (https-only, no obvious header gaps)
  3. /api/health returns ok and reports loaded chunks > 0
  4. Latency budget on /api/health (cold-cache + warm)
  5. Latency budget on /api/chat (it's a CPU LLM, so be generous)
  6. Knowledge tests — questions whose answers are documented should
     produce responses citing the right sources and containing expected
     keywords. No hallucinations on documented topics.
  7. Negative tests — questions OUTSIDE the docs should refuse politely
     instead of making things up.
  8. Input validation — empty / oversize queries return 4xx, not 500.

Usage:
    python tests/verify_deployment.py https://chat.build.openmesh.cloud
    python tests/verify_deployment.py https://chat.build.openmesh.cloud --json

Exit code is 0 if every check passes, non-zero otherwise. CI-friendly.

This file has no external dependencies beyond `requests`. Run it on a
laptop, in CI, or shell into the xnode and run it locally.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

# Latency budgets in seconds. The CPU LLM is slow on first request after a
# cold start (it loads model weights into RAM); subsequent requests are
# faster. We measure both.
LATENCY_HEALTH_MAX = 5.0
LATENCY_CHAT_COLD_MAX = 90.0   # first call after deploy: model load + answer
LATENCY_CHAT_WARM_MAX = 60.0   # subsequent calls

# Questions that ARE in the docs corpus. The agent must answer with at
# least one of `must_contain_any` keywords (case-insensitive substring) and
# must not return one of `must_not_contain` strings (which indicate either
# refusal or empty response). Each test also requires at least N sources.
KNOWLEDGE_TESTS = [
    {
        "name": "deploy_basic",
        "query": "How do I deploy an app to my Xnode using the om CLI?",
        "must_contain_any": ["om app deploy", "--flake"],
        "must_not_contain": ["I don't have", "not in the docs"],
        "min_sources": 1,
    },
    {
        "name": "expose_subdomain",
        "query": "How do I expose a deployed container on a public subdomain?",
        "must_contain_any": ["om app expose", "--domain", "--port"],
        "must_not_contain": ["I don't have"],
        "min_sources": 1,
    },
    {
        "name": "error_session_expired",
        "query": "What does the error code E_SESSION_EXPIRED mean and how do I fix it?",
        "must_contain_any": ["om login", "re-authenticate", "session"],
        "must_not_contain": ["I don't have"],
        "min_sources": 1,
    },
    {
        "name": "json_format",
        "query": "How do I get JSON output from the om CLI for use in scripts?",
        "must_contain_any": ["--format json", "format json"],
        "min_sources": 1,
    },
    {
        "name": "what_is_xnode",
        "query": "What is a sovereign Xnode?",
        "must_contain_any": ["xnode", "sovereign", "decentralized", "decentralised"],
        "min_sources": 1,
    },
]

# Questions that are NOT in the docs corpus. The agent should refuse
# politely rather than make stuff up.
NEGATIVE_TESTS = [
    {
        "name": "off_topic_crypto",
        "query": "What is the current price of Bitcoin?",
        "must_contain_any": [
            "don't have",
            "not in the docs",
            "check",
            "documentation",
            "i don't",
            "do not have",
        ],
    },
    {
        "name": "off_topic_creative",
        "query": "Write me a short poem about cats.",
        "must_contain_any": [
            "don't have",
            "not in the docs",
            "check",
            "documentation",
            "i don't",
            "do not have",
        ],
    },
]

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    duration_s: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class Suite:
    base_url: str
    json_output: bool = False
    results: list[CheckResult] = field(default_factory=list)

    def record(self, result: CheckResult) -> None:
        self.results.append(result)
        if not self.json_output:
            mark = "✓" if result.passed else "✗"
            line = f"  [{mark}] {result.name} ({result.duration_s:.2f}s)"
            if result.detail:
                line += f" — {result.detail}"
            print(line)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def section(self, title: str) -> None:
        if not self.json_output:
            print(f"\n{title}")
            print("-" * len(title))


def run_check(
    suite: Suite, name: str, fn: Callable[[], CheckResult]
) -> CheckResult:
    start = time.monotonic()
    try:
        result = fn()
    except AssertionError as e:
        result = CheckResult(name=name, passed=False, detail=str(e))
    except Exception as e:  # noqa: BLE001
        result = CheckResult(name=name, passed=False, detail=f"unexpected error: {e}")
    if result.duration_s == 0.0:
        result.duration_s = time.monotonic() - start
    suite.record(result)
    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_https_reachable(base_url: str) -> CheckResult:
    if not base_url.startswith("https://"):
        return CheckResult(
            name="https_only",
            passed=False,
            detail=f"base URL is not https:// — got {base_url}",
        )
    r = requests.get(base_url, timeout=15)
    return CheckResult(
        name="https_reachable",
        passed=r.status_code == 200,
        detail=f"GET / -> {r.status_code}",
        extra={"status": r.status_code, "bytes": len(r.content)},
    )


def check_tls_valid(base_url: str) -> CheckResult:
    # requests.get with verify=True (default) raises on bad TLS.
    try:
        requests.get(base_url, timeout=15, verify=True)
        return CheckResult(name="tls_valid", passed=True, detail="cert validates")
    except requests.exceptions.SSLError as e:
        return CheckResult(name="tls_valid", passed=False, detail=str(e))


def check_security_headers(base_url: str) -> CheckResult:
    r = requests.get(base_url, timeout=15)
    headers = {k.lower(): v for k, v in r.headers.items()}
    # We don't fail the suite if security headers are missing — that's a
    # configuration to-do, not a deployment failure. But we report.
    findings = []
    if "strict-transport-security" not in headers:
        findings.append("no HSTS header")
    if "x-content-type-options" not in headers:
        findings.append("no X-Content-Type-Options")
    detail = "all good" if not findings else "; ".join(findings)
    # PASS this as a soft warning — print findings but don't fail.
    return CheckResult(
        name="security_headers",
        passed=True,
        detail=detail,
        extra={"missing": findings},
    )


def check_health(base_url: str) -> CheckResult:
    start = time.monotonic()
    r = requests.get(f"{base_url}/api/health", timeout=10)
    duration = time.monotonic() - start
    if r.status_code != 200:
        return CheckResult(
            name="health",
            passed=False,
            detail=f"status {r.status_code}",
            duration_s=duration,
        )
    data = r.json()
    if data.get("status") != "ok":
        return CheckResult(
            name="health",
            passed=False,
            detail=f"status field is {data.get('status')!r}",
            duration_s=duration,
        )
    chunks = data.get("chunks_loaded", 0)
    if chunks < 1:
        return CheckResult(
            name="health",
            passed=False,
            detail=f"chunks_loaded={chunks} (corpus is empty)",
            duration_s=duration,
        )
    return CheckResult(
        name="health",
        passed=True,
        detail=f"{chunks} chunks loaded, model={data.get('chat_model')}",
        duration_s=duration,
        extra=data,
    )


def check_latency_health(base_url: str) -> CheckResult:
    times = []
    for _ in range(3):
        start = time.monotonic()
        r = requests.get(f"{base_url}/api/health", timeout=10)
        times.append(time.monotonic() - start)
        assert r.status_code == 200, f"health returned {r.status_code}"
    avg = sum(times) / len(times)
    return CheckResult(
        name="latency_health",
        passed=avg <= LATENCY_HEALTH_MAX,
        detail=f"avg {avg*1000:.0f}ms over 3 calls (budget: {LATENCY_HEALTH_MAX}s)",
        duration_s=avg,
        extra={"samples_ms": [round(t * 1000) for t in times]},
    )


def post_chat(base_url: str, query: str, timeout: int = 120) -> tuple[dict, float]:
    start = time.monotonic()
    r = requests.post(
        f"{base_url}/api/chat",
        json={"query": query},
        timeout=timeout,
    )
    duration = time.monotonic() - start
    if r.status_code != 200:
        raise AssertionError(f"chat returned {r.status_code}: {r.text[:200]}")
    return r.json(), duration


def check_chat_cold(base_url: str) -> CheckResult:
    # First chat after deploy. Model loads into RAM here, so it's slow.
    data, duration = post_chat(base_url, "What is the om CLI?", timeout=120)
    if data.get("error"):
        return CheckResult(
            name="chat_cold",
            passed=False,
            detail=f"error: {data['error']}",
            duration_s=duration,
        )
    answer = data.get("answer", "")
    return CheckResult(
        name="chat_cold",
        passed=duration <= LATENCY_CHAT_COLD_MAX and len(answer) > 10,
        detail=f"{duration:.1f}s, answer {len(answer)} chars (budget: {LATENCY_CHAT_COLD_MAX}s)",
        duration_s=duration,
    )


def check_chat_warm(base_url: str) -> CheckResult:
    data, duration = post_chat(base_url, "How do I list deployed apps?", timeout=90)
    if data.get("error"):
        return CheckResult(
            name="chat_warm",
            passed=False,
            detail=f"error: {data['error']}",
            duration_s=duration,
        )
    return CheckResult(
        name="chat_warm",
        passed=duration <= LATENCY_CHAT_WARM_MAX,
        detail=f"{duration:.1f}s (budget: {LATENCY_CHAT_WARM_MAX}s)",
        duration_s=duration,
    )


def check_input_validation(base_url: str) -> CheckResult:
    findings = []
    # Empty query
    r = requests.post(f"{base_url}/api/chat", json={"query": ""}, timeout=10)
    if r.status_code != 400:
        findings.append(f"empty query returned {r.status_code}, expected 400")
    # Oversize query
    r = requests.post(f"{base_url}/api/chat", json={"query": "x" * 5000}, timeout=10)
    if r.status_code != 400:
        findings.append(f"oversize query returned {r.status_code}, expected 400")
    return CheckResult(
        name="input_validation",
        passed=not findings,
        detail="all good" if not findings else "; ".join(findings),
    )


def check_knowledge(base_url: str, test: dict) -> CheckResult:
    name = f"knowledge:{test['name']}"
    try:
        data, duration = post_chat(base_url, test["query"], timeout=120)
    except AssertionError as e:
        return CheckResult(name=name, passed=False, detail=str(e))

    if data.get("error"):
        return CheckResult(
            name=name,
            passed=False,
            detail=f"chat error: {data['error']}",
            duration_s=duration,
        )

    answer = (data.get("answer") or "").lower()
    sources = data.get("sources") or []

    findings = []
    if test.get("min_sources", 0) > 0 and len(sources) < test["min_sources"]:
        findings.append(f"got {len(sources)} sources, need {test['min_sources']}")

    must_contain_any = test.get("must_contain_any", [])
    if must_contain_any and not any(kw.lower() in answer for kw in must_contain_any):
        findings.append(
            f"answer missing all of {must_contain_any}; got: {answer[:120]!r}"
        )

    must_not_contain = test.get("must_not_contain", [])
    bad = [kw for kw in must_not_contain if kw.lower() in answer]
    if bad:
        findings.append(f"answer contains {bad}; got: {answer[:120]!r}")

    return CheckResult(
        name=name,
        passed=not findings,
        detail="ok" if not findings else "; ".join(findings),
        duration_s=duration,
        extra={"answer_preview": (data.get("answer") or "")[:200], "sources": sources},
    )


def check_negative(base_url: str, test: dict) -> CheckResult:
    name = f"negative:{test['name']}"
    try:
        data, duration = post_chat(base_url, test["query"], timeout=120)
    except AssertionError as e:
        return CheckResult(name=name, passed=False, detail=str(e))

    if data.get("error"):
        return CheckResult(
            name=name,
            passed=False,
            detail=f"chat error: {data['error']}",
            duration_s=duration,
        )

    answer = (data.get("answer") or "").lower()
    must_contain_any = test.get("must_contain_any", [])
    if not any(kw.lower() in answer for kw in must_contain_any):
        return CheckResult(
            name=name,
            passed=False,
            detail=(
                f"agent did not refuse — answer should contain one of "
                f"{must_contain_any}; got: {answer[:200]!r}"
            ),
            duration_s=duration,
        )
    return CheckResult(
        name=name,
        passed=True,
        detail="agent refused appropriately",
        duration_s=duration,
        extra={"answer_preview": (data.get("answer") or "")[:200]},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="https://chat.your-domain.example")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human output",
    )
    parser.add_argument(
        "--skip-knowledge",
        action="store_true",
        help="Skip knowledge tests (faster, fewer LLM calls)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    suite = Suite(base_url=base_url, json_output=args.json)

    if not args.json:
        print(f"Verifying deployment at {base_url}")

    suite.section("Transport & TLS")
    run_check(suite, "https_reachable", lambda: check_https_reachable(base_url))
    run_check(suite, "tls_valid", lambda: check_tls_valid(base_url))
    run_check(suite, "security_headers", lambda: check_security_headers(base_url))

    suite.section("Health & corpus")
    run_check(suite, "health", lambda: check_health(base_url))
    run_check(suite, "latency_health", lambda: check_latency_health(base_url))

    suite.section("Input validation")
    run_check(suite, "input_validation", lambda: check_input_validation(base_url))

    suite.section("Chat latency")
    run_check(suite, "chat_cold", lambda: check_chat_cold(base_url))
    run_check(suite, "chat_warm", lambda: check_chat_warm(base_url))

    if not args.skip_knowledge:
        suite.section("Knowledge tests (the agent should know these)")
        for test in KNOWLEDGE_TESTS:
            run_check(
                suite,
                f"knowledge:{test['name']}",
                lambda t=test: check_knowledge(base_url, t),
            )

        suite.section("Negative tests (the agent should refuse these)")
        for test in NEGATIVE_TESTS:
            run_check(
                suite,
                f"negative:{test['name']}",
                lambda t=test: check_negative(base_url, t),
            )

    if args.json:
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "passed": suite.passed,
                    "results": [
                        {
                            "name": r.name,
                            "passed": r.passed,
                            "detail": r.detail,
                            "duration_s": round(r.duration_s, 3),
                            "extra": r.extra,
                        }
                        for r in suite.results
                    ],
                },
                indent=2,
            )
        )
    else:
        passed = sum(1 for r in suite.results if r.passed)
        total = len(suite.results)
        print(f"\n{passed}/{total} checks passed")
        if not suite.passed:
            print("\nFailures:")
            for r in suite.results:
                if not r.passed:
                    print(f"  ✗ {r.name}: {r.detail}")

    return 0 if suite.passed else 1


if __name__ == "__main__":
    sys.exit(main())
