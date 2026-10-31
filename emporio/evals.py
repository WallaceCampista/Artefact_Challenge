"""End-to-end evaluation against the configured model: `python -m emporio eval`.

Two parts:
1. Policy retrieval (no LLM): hit@1 / hit@3 of the configured search over
   evals/policy_retrieval.json, compared with BM25 alone.
2. Agent cases (evals/cases.json): each case is a short conversation; the last answer is checked
   for tool routing (expected / forbidden tools), required and forbidden content, and grounding.

The clock is frozen (default: a Tuesday morning, store open) so results are reproducible;
a case can set its own "now" to test closed hours and holidays.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from .agent import Agent, ModelUnavailable
from .app import build_agent
from .config import PROJECT_ROOT, Settings
from .policies import PolicyIndex
from .text import strip_accents

CASES = PROJECT_ROOT / "evals" / "cases.json"
RETRIEVAL_CASES = PROJECT_ROOT / "evals" / "policy_retrieval.json"
REPORT = PROJECT_ROOT / "evals" / "report.md"
DEFAULT_NOW = "2026-11-03T10:30:00"


def _norm(text: str) -> str:
    return " ".join(strip_accents(text).lower().split())


def _contains(answer: str, expected) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(_norm(option) in _norm(answer) for option in options)


def check_case(case: dict, answer: str, tools_used: list[str], ungrounded: list[str]) -> list[str]:
    """Returns the list of failed checks (empty = pass)."""
    failures = []
    expected = case.get("expect_tools")
    if expected and not set(expected) & set(tools_used):
        failures.append(f"expected one of {expected}, used {tools_used or 'none'}")
    for tool in case.get("forbid_tools", []):
        if tool in tools_used:
            failures.append(f"must not call {tool}")
    for item in case.get("must_include", []):
        if not _contains(answer, item):
            failures.append(f"missing {item}")
    for item in case.get("must_not_include", []):
        if _contains(answer, item):
            failures.append(f"must not say '{item}'")
    if ungrounded:
        failures.append(f"ungrounded {ungrounded}")
    return failures


def run_retrieval(agent: Agent) -> dict:
    index = agent.tools.policies
    baseline = PolicyIndex(index.sections)  # BM25 only
    cases = json.loads(RETRIEVAL_CASES.read_text())
    scores = {}
    for name, idx in (("BM25", baseline), (index.mode, index)):
        hit1 = hit3 = 0
        for case in cases:
            found = [s.number for s in idx.search(case["question"], k=3)]
            hit1 += found[0] in case["expected"]
            hit3 += any(n in case["expected"] for n in found)
        scores[name] = (hit1, hit3, len(cases))
    return scores


def run_cases(settings: Settings, only: str | None = None) -> list[dict]:
    cases = json.loads(CASES.read_text())
    if only:
        cases = [c for c in cases if c["id"] == only]
    agents: dict[str, Agent] = {}
    results = []
    for case in cases:
        now = case.get("now", DEFAULT_NOW)
        if now not in agents:
            agents[now] = build_agent(dataclasses.replace(settings, reference_datetime=now))
        agent = agents[now]
        history: list[dict] = []
        started = time.perf_counter()
        try:
            for turn in case["turns"]:
                reply = agent.respond(history, turn, session_id=f"eval-{case['id']}")
                history += [{"role": "user", "content": turn}, {"role": "assistant", "content": reply.text}]
        except ModelUnavailable as exc:
            raise SystemExit(f"model unavailable: {exc}") from exc
        tools_used = [c.name for c in reply.tool_calls]
        failures = check_case(case, reply.text, tools_used, reply.ungrounded)
        result = {
            "id": case["id"], "group": case["group"], "passed": not failures, "failures": failures,
            "tools": tools_used, "answer": reply.text, "retries": reply.grounding_retries,
            "seconds": time.perf_counter() - started,
            "routing_ok": not any(f.startswith(("expected", "must not call")) for f in failures),
        }
        results.append(result)
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"[{mark}] {case['id']:<24} {result['seconds']:5.1f}s  tools={tools_used}"
              + (f"  -> {'; '.join(failures)}" if failures else ""), flush=True)
    return results


def write_report(settings: Settings, retrieval: dict, results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    routing = sum(r["routing_ok"] for r in results)
    retries = sum(r["retries"] for r in results)
    avg = sum(r["seconds"] for r in results) / max(len(results), 1)
    lines = [
        "# Evaluation report",
        "",
        f"Model: `{settings.llm_model}` · embeddings: `{settings.embedding_model or 'none'}` · "
        f"temperature {settings.llm_temperature} · clock frozen at {DEFAULT_NOW} unless the case sets it",
        "",
        "## Policy retrieval (22 questions, top-3 sections)",
        "",
        "| Search | hit@1 | hit@3 |",
        "|---|---|---|",
        *[f"| {name} | {h1}/{n} | {h3}/{n} |" for name, (h1, h3, n) in retrieval.items()],
        "",
        "## Agent cases",
        "",
        f"**{passed}/{len(results)} passed** · tool routing {routing}/{len(results)} · "
        f"grounding retries {retries} · average {avg:.1f} s per case",
        "",
        "| Case | Group | Result | Tools | Notes |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        notes = "; ".join(r["failures"]).replace("|", "/") or ("1 grounding retry" if r["retries"] else "")
        lines.append(f"| `{r['id']}` | {r['group']} | {'pass' if r['passed'] else '**fail**'} | "
                     f"{', '.join(r['tools']) or '-'} | {notes} |")
    lines += ["", "## Answers", ""]
    for r in results:
        lines += [f"### {r['id']} ({'pass' if r['passed'] else 'fail'})", "", "> " + r["answer"].replace("\n", "\n> "), ""]
    return "\n".join(lines)


def run_eval(settings: Settings, only: str | None = None, save: bool = True) -> int:
    print(f"Model {settings.llm_model} at {settings.llm_base_url}\n")
    agent = build_agent(dataclasses.replace(settings, reference_datetime=DEFAULT_NOW))
    retrieval = run_retrieval(agent)
    for name, (h1, h3, n) in retrieval.items():
        print(f"policy retrieval [{name}]: hit@1 {h1}/{n}  hit@3 {h3}/{n}")
    print()
    results = run_cases(settings, only)
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} cases passed")
    if save and not only:
        REPORT.write_text(write_report(settings, retrieval, results), encoding="utf-8")
        print(f"report written to {REPORT.relative_to(PROJECT_ROOT)}")
    return 0 if passed == len(results) else 1
