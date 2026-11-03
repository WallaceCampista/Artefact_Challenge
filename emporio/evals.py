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
import re
import time

from .agent import Agent, ModelUnavailable, session_facts
from .app import build_agent
from .config import PROJECT_ROOT, Settings
from .policies import PolicyIndex
from .text import strip_accents

CASES = PROJECT_ROOT / "evals" / "cases.json"
RETRIEVAL_CASES = PROJECT_ROOT / "evals" / "policy_retrieval.json"
REPORT = PROJECT_ROOT / "evals" / "report.md"
DEFAULT_NOW = "2026-11-03T10:30:00"
# Guard outcomes that changed the final answer, as they appear in the report's notes.
GUARD_NOTES = {
    ("grounding", "retry"): "grounding retry",
    ("grounding", "unresolved"): "grounding unresolved",
    ("grounding", "withheld"): "price without a source withheld",
    ("protocol", "appended"): "protocol added by code",
    ("promise", "removed"): "unverified yes removed",
    ("language", "trimmed"): "other-language text trimmed",
    ("personal_data", "redacted"): "personal data masked",
    ("system_prompt", "blocked"): "system prompt leak blocked",
}


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
    for pattern in case.get("must_not_match", []):  # e.g. any e-mail address or phone number
        found = re.search(pattern, answer, re.IGNORECASE)
        if found:
            failures.append(f"must not contain '{found.group(0)}'")
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


def _run_once(agent: Agent, case: dict) -> dict:
    history: list[dict] = []
    trail: list = []
    started = time.perf_counter()
    try:
        for turn in case["turns"]:  # same flow as ChatService.send, without the database
            reply = agent.respond(history, turn, session_id=f"eval-{case['id']}", facts=session_facts(trail))
            history += [{"role": "user", "content": turn}, {"role": "assistant", "content": reply.text}]
            trail += reply.tool_calls
    except ModelUnavailable as exc:
        raise SystemExit(f"model unavailable: {exc}") from exc
    tools_used = [c.name for c in reply.tool_calls]
    failures = check_case(case, reply.text, tools_used, reply.ungrounded)
    return {
        "failures": failures, "tools": tools_used, "answer": reply.text, "retries": reply.grounding_retries,
        "redactions": len(reply.redacted), "seconds": time.perf_counter() - started,
        "guards": [GUARD_NOTES[key] for e in reply.trace if e["type"] == "guard"
                   and (key := (e["check"], e["outcome"])) in GUARD_NOTES],
        "routing_ok": not any(f.startswith(("expected", "must not call")) for f in failures),
    }


def run_cases(settings: Settings, only: str | None = None, repeat: int = 1) -> list[dict]:
    """Runs every case `repeat` times. A case passes only if every run passes; with repeat > 1
    the report also shows which cases are flaky (pass in some runs, fail in others)."""
    cases = json.loads(CASES.read_text())
    if only:
        cases = [c for c in cases if c["id"] == only]
    agents: dict[str, Agent] = {}
    results = []
    for case in cases:
        now = case.get("now", DEFAULT_NOW)
        if now not in agents:
            agents[now] = build_agent(dataclasses.replace(settings, reference_datetime=now))
        runs = [_run_once(agents[now], case) for _ in range(repeat)]
        passes = sum(not r["failures"] for r in runs)
        shown = next((r for r in runs if r["failures"]), runs[0])  # show a failing run if any
        result = {
            "id": case["id"], "group": case["group"], "passed": passes == repeat, "passes": passes,
            "runs": repeat, "failures": shown["failures"], "tools": shown["tools"], "answer": shown["answer"],
            "retries": sum(r["retries"] for r in runs), "redactions": sum(r["redactions"] for r in runs),
            "seconds": sum(r["seconds"] for r in runs) / repeat,
            "routing_ok": all(r["routing_ok"] for r in runs),
            "guards": {note: sum(note in r["guards"] for r in runs)
                       for note in dict.fromkeys(n for r in runs for n in r["guards"])},
        }
        results.append(result)
        if repeat == 1:
            mark = "PASS" if result["passed"] else "FAIL"
        else:
            mark = ("PASS" if passes == repeat else "FLAKY" if passes else "FAIL") + f" {passes}/{repeat}"
        print(f"[{mark}] {case['id']:<26} {result['seconds']:5.1f}s  tools={result['tools']}"
              + (f"  -> {'; '.join(result['failures'])}" if result["failures"] else ""), flush=True)
    return results


def write_report(settings: Settings, retrieval: dict, results: list[dict]) -> str:
    repeat = results[0]["runs"] if results else 1
    passed = sum(r["passed"] for r in results)
    routing = sum(r["routing_ok"] for r in results)
    retries = sum(r["retries"] for r in results)
    redactions = sum(r["redactions"] for r in results)
    avg = sum(r["seconds"] for r in results) / max(len(results), 1)
    runs_passed = sum(r["passes"] for r in results)
    total_runs = sum(r["runs"] for r in results)
    flaky = [r["id"] for r in results if 0 < r["passes"] < r["runs"]]
    blocked = sum(r["guards"].get(GUARD_NOTES[("system_prompt", "blocked")], 0) for r in results)
    lines = [
        "# Evaluation report",
        "",
        f"Model: `{settings.llm_model}` · embeddings: `{settings.embedding_model or 'none'}` · "
        f"temperature {settings.llm_temperature} · clock frozen at {DEFAULT_NOW} unless the case sets it"
        + (f" · each case run {repeat} times" if repeat > 1 else ""),
        "",
        "## Policy retrieval (22 questions, top-3 sections)",
        "",
        "| Search | hit@1 | hit@3 |",
        "|---|---|---|",
        *[f"| {name} | {h1}/{n} | {h3}/{n} |" for name, (h1, h3, n) in retrieval.items()],
        "",
        "## Agent cases",
        "",
        f"**{passed}/{len(results)} passed{' in every run' if repeat > 1 else ''}** · tool routing "
        f"{routing}/{len(results)} · grounding retries {retries} · personal data redactions {redactions} · "
        f"prompt leaks blocked {blocked} · "
        f"average {avg:.1f} s per case",
    ]
    if repeat > 1:
        lines += ["", f"Consistency: {runs_passed}/{total_runs} runs passed "
                      f"({100 * runs_passed / total_runs:.0f}%) · flaky cases: {', '.join(flaky) or 'none'}"]
    lines += [
        "",
        "| Case | Group | Result | Tools | Notes |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        guards = [note + (f" {n}/{r['runs']}" if r["runs"] > 1 else "") for note, n in r["guards"].items()]
        notes = "; ".join(r["failures"] + guards).replace("|", "/")
        outcome = "pass" if r["passed"] else ("**flaky**" if r["passes"] else "**fail**")
        if r["runs"] > 1:
            outcome += f" {r['passes']}/{r['runs']}"
        lines.append(f"| `{r['id']}` | {r['group']} | {outcome} | {', '.join(r['tools']) or '-'} | {notes} |")
    lines += ["", "## Answers", "", "One run per case; a failing run is shown when there is one.", ""]
    for r in results:
        lines += [f"### {r['id']} ({'pass' if r['passed'] else 'fail'})", "", "> " + r["answer"].replace("\n", "\n> "), ""]
    return "\n".join(lines)


def run_eval(settings: Settings, only: str | None = None, save: bool = True, repeat: int = 1) -> int:
    print(f"Model {settings.llm_model} at {settings.llm_base_url}\n")
    agent = build_agent(dataclasses.replace(settings, reference_datetime=DEFAULT_NOW))
    retrieval = run_retrieval(agent)
    for name, (h1, h3, n) in retrieval.items():
        print(f"policy retrieval [{name}]: hit@1 {h1}/{n}  hit@3 {h3}/{n}")
    print()
    results = run_cases(settings, only, repeat)
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} cases passed" + (" in every run" if repeat > 1 else ""))
    if repeat > 1:
        runs_passed = sum(r["passes"] for r in results)
        print(f"consistency: {runs_passed}/{sum(r['runs'] for r in results)} runs passed")
    if save and not only:
        REPORT.write_text(write_report(settings, retrieval, results), encoding="utf-8")
        print(f"report written to {REPORT.relative_to(PROJECT_ROOT)}")
    return 0 if passed == len(results) else 1
