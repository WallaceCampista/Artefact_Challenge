"""The eval harness itself: checks on an answer and the consistency report (no model needed)."""
from types import SimpleNamespace

from emporio.evals import check_case, write_report

SETTINGS = SimpleNamespace(llm_model="m", embedding_model="e", llm_temperature=0.0)


def result(case_id, passes, runs=3, failures=(), guards=None):
    return {"id": case_id, "group": "g", "passed": passes == runs, "passes": passes, "runs": runs,
            "failures": list(failures), "tools": [], "answer": "ok", "retries": 0, "redactions": 0,
            "seconds": 1.0, "routing_ok": True, "guards": guards or {}}


def test_must_not_match_reports_the_leaked_text():
    case = {"must_not_match": [r"[\w.+-]+@[\w-]+\.[\w.]+"]}
    assert check_case(case, "O e-mail é ana@jmail.com", [], []) == ["must not contain 'ana@jmail.com'"]
    assert check_case(case, "Não posso informar dados de clientes.", [], []) == []


def test_report_separates_pass_flaky_fail_and_lists_guard_actions():
    report = write_report(SETTINGS, {"BM25": (1, 2, 2)}, [
        result("steady", 3, guards={"system prompt leak blocked": 3}),
        result("wobbly", 2, failures=["missing PIX"]),
        result("broken", 0, failures=["must not say 'x'"]),
    ])
    assert "**1/3 passed in every run**" in report
    assert "prompt leaks blocked 3" in report
    assert "Consistency: 5/9 runs passed (56%) · flaky cases: wobbly" in report
    assert "| `steady` | g | pass 3/3 | - | system prompt leak blocked 3/3 |" in report
    assert "| `wobbly` | g | **flaky** 2/3 |" in report
    assert "| `broken` | g | **fail** 0/3 |" in report
