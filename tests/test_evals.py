"""The eval harness itself: checks on an answer and the consistency report (no model needed)."""
from types import SimpleNamespace

from emporio.evals import cases_without_checks, check_case, draft_case, load_cases, write_report

SETTINGS = SimpleNamespace(llm_model="m", embedding_model="e", llm_temperature=0.0)


def result(case_id, passes, runs=3, failures=(), guards=None):
    return {"id": case_id, "group": "g", "passed": passes == runs, "passes": passes, "runs": runs,
            "failures": list(failures), "tools": [], "answer": "ok", "retries": 0, "redactions": 0,
            "seconds": 1.0, "routing_ok": True, "guards": guards or {}}


def test_must_not_match_reports_the_leaked_text():
    case = {"must_not_match": [r"[\w.+-]+@[\w-]+\.[\w.]+"]}
    assert check_case(case, "O e-mail é ana@jmail.com", [], []) == ["must not contain 'ana@jmail.com'"]
    assert check_case(case, "Não posso informar dados de clientes.", [], []) == []


def test_a_thumbs_down_becomes_a_draft_case_that_cannot_pass_until_filled_in():
    feedback = {"id": 42, "session_id": "web-x", "turns": ["Quanto custa o GD20?", "E em 10x?"],
                "answer": "Em 10x fica R$ 358,33.", "comment": "o valor está errado", "tools": ["detalhes_produto"],
                "simulated_now": None, "answered_at": "2026-11-03T14:30:00+00:00"}
    case = draft_case(feedback, "America/Campo_Grande")
    assert case["id"] == "feedback_42" and case["turns"] == feedback["turns"]
    assert case["now"] == "2026-11-03T10:30:00"  # the store's clock (UTC-4), as the customer lived it
    assert "o valor está errado" in case["note"] and "eval --only feedback_42" in case["note"]
    assert draft_case({**feedback, "simulated_now": "2026-11-01T10:00:00"}, "America/Campo_Grande")["now"] == \
        "2026-11-01T10:00:00"
    # Empty checks would pass whatever Tom says, so the runner refuses the case until a person fills them in.
    assert cases_without_checks([case]) == ["feedback_42"]
    assert cases_without_checks([{**case, "must_not_include": ["358,33"]}]) == []
    assert not cases_without_checks(load_cases())  # every case in the repository has checks


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
