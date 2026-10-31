import json

import pytest

from emporio.config import PROJECT_ROOT
from emporio.policies import PolicyIndex, parse_sections

PDF = PROJECT_ROOT / "data" / "políticas_da_loja.pdf"
CASES = json.loads((PROJECT_ROOT / "evals" / "policy_retrieval.json").read_text())


@pytest.fixture(scope="module")
def sections():
    return parse_sections(PDF)


def test_sections_follow_the_manual_numbering(sections):
    numbers = [s.number for s in sections]
    assert numbers[:3] == ["1", "1.1", "1.2"]
    assert {"2", "3.1", "4.1", "4.4", "5.2", "7.3", "8.3", "9", "10"} <= set(numbers)
    # Numbered list items of 7.2 ("1. Saudação") stay inside 7.2 instead of becoming headings.
    flow = next(s for s in sections if s.number == "7.2")
    assert "1. Saudação" in flow.body and "5. Fechamento" in flow.body


def test_headers_footers_and_wrapping_are_cleaned(sections):
    text = "\n".join(s.body for s in sections)
    assert "Página" not in text and "Manual de Políticas e Procedimentos" not in text
    regret = next(s for s in sections if s.number == "4.1")
    assert "- O cliente pode solicitar a devolução em até 7 (sete) dias corridos após o recebimento do produto" in regret.body


def test_bm25_alone_finds_the_right_section_in_top3(sections):
    """Offline baseline (no embedding server). Hybrid search is measured by `python -m emporio eval`."""
    index = PolicyIndex(sections)
    hits = sum(
        any(s.number in case["expected"] for s in index.search(case["question"], k=3)) for case in CASES
    )
    assert hits / len(CASES) >= 0.9, f"hit@3 = {hits}/{len(CASES)}"


def test_falls_back_to_bm25_when_embeddings_fail(sections):
    class Broken:
        model = "broken"

        def documents(self, texts):
            raise ConnectionError("server down")

    index = PolicyIndex(sections, Broken())
    assert index.mode == "BM25"
    assert index.search("posso parcelar em 10 vezes?")[0].number in ("3.1", "3")
