"""Retrieval over the store's policy manual (`data/políticas_da_loja.pdf`).

Pipeline: PDF -> clean text -> one chunk per numbered section -> hybrid search.
- Chunking by section: the manual is short (8 pages) and already organised in numbered
  sections, so a section is the natural unit of meaning and gives a citable source ("4.1").
- Hybrid search: BM25 catches exact terms ("PIX", "SEDEX", "boleto"); embeddings catch
  paraphrases ("quanto tempo demora pra chegar" -> delivery deadlines). Both rankings are
  merged with Reciprocal Rank Fusion, which needs no score calibration between them.
- Without an embedding model (or if the server fails), search falls back to BM25 alone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .text import BM25, stems

log = logging.getLogger(__name__)

HEADER = "Empório da Música Manual de Políticas e Procedimentos"
HEADING = re.compile(r"^(\d+)(?:\.(\d+))?\.?\s+(\S.*)$")
RRF_K = 60


@dataclass(frozen=True)
class PolicySection:
    number: str  # "4.1"
    title: str  # "Direito de Arrependimento (Compras Online)"
    parent: str  # "4. Política de Trocas e Devoluções" ("" for top-level sections)
    body: str

    @property
    def label(self) -> str:
        return f"{self.number} {self.title}"

    @property
    def text(self) -> str:
        """What gets indexed and shown to the model: hierarchy + content."""
        heading = f"{self.parent} > {self.label}" if self.parent else self.label
        return f"{heading}\n{self.body}"


def _clean_page(raw: str) -> list[str]:
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped == HEADER or re.fullmatch(r"Página \d+", stripped):
            continue
        indented = line.startswith(" ")
        stripped = re.sub(r"^(•|\d+\.)\s+", r"\1 ", stripped)  # list markers keep one space
        # Table rows come out as columns separated by runs of spaces.
        stripped = re.sub(r"\s{3,}", " | ", stripped)
        lines.append(("  " if indented else "") + stripped)
    return lines


LIST_ITEM = re.compile(r"^\s*(•|\d+\.)\s")


def _join_wrapped(lines: list[str]) -> list[str]:
    """List items wrapped over several (indented) lines become one line each."""
    joined: list[str] = []
    for line in lines:
        wraps_previous = (
            joined and line.startswith("  ") and not LIST_ITEM.match(line) and LIST_ITEM.match(joined[-1])
        )
        if wraps_previous:
            joined[-1] = f"{joined[-1]} {line.strip()}"
        else:
            joined.append(line)
    return [line.strip().replace("•", "-") for line in joined]


def parse_sections(pdf_path: Path) -> list[PolicySection]:
    reader = PdfReader(str(pdf_path))
    lines: list[str] = []
    for page in reader.pages[1:]:  # page 1 is the cover
        lines += _clean_page(page.extract_text(extraction_mode="layout"))
    lines = _join_wrapped(lines)

    sections: list[PolicySection] = []
    top_number, top_title = 0, ""
    current: dict | None = None

    def flush():
        if current and current["body"]:
            sections.append(PolicySection(current["number"], current["title"], current["parent"],
                                          "\n".join(current["body"])))

    for line in lines:
        match = HEADING.match(line)
        # A heading must follow the numbering (next top-level number, or a subsection of the
        # current one); this keeps numbered lists like "1. Saudação: ..." inside their section.
        if match and not line.startswith("-"):
            major, minor, title = int(match.group(1)), match.group(2), match.group(3)
            if minor is None and major == top_number + 1:
                flush()
                top_number, top_title = major, title
                current = {"number": str(major), "title": title, "parent": "", "body": []}
                continue
            if minor is not None and major == top_number:
                flush()
                current = {"number": f"{major}.{minor}", "title": title,
                           "parent": f"{top_number}. {top_title}", "body": []}
                continue
        if current is not None:
            current["body"].append(line)
    flush()
    return sections


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class PolicyIndex:
    def __init__(self, sections: list[PolicySection], embedder=None, cache_dir: Path | None = None):
        self.sections = sections
        # Titles count twice: they are the best summary of what a section is about.
        self.bm25 = BM25([stems(f"{s.title} {s.title} {s.parent} {s.body}") for s in sections])
        self.embedder = None
        self.vectors: list[list[float]] = []
        if embedder is not None:
            try:
                self.vectors = self._load_vectors(embedder, cache_dir)
                self.embedder = embedder
            except Exception as exc:  # server down, model not loaded...
                log.warning("embeddings unavailable, policy search uses BM25 only: %s", exc)

    @classmethod
    def from_pdf(cls, pdf_path: Path, embedder=None, cache_dir: Path | None = None) -> "PolicyIndex":
        return cls(parse_sections(pdf_path), embedder, cache_dir)

    @property
    def mode(self) -> str:
        return "hybrid (BM25 + embeddings, RRF)" if self.embedder else "BM25"

    def _load_vectors(self, embedder, cache_dir: Path | None) -> list[list[float]]:
        texts = [s.text for s in self.sections]
        key = hashlib.sha256(json.dumps([embedder.model, texts]).encode()).hexdigest()[:16]
        cache = cache_dir / f"policy-embeddings-{key}.json" if cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text())
        vectors = embedder.documents(texts)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(vectors))
        return vectors

    def search(self, query: str, k: int = 3) -> list[PolicySection]:
        rankings = []
        bm25_scores = self.bm25.scores(stems(query))
        rankings.append([i for i in sorted(range(len(self.sections)), key=lambda i: -bm25_scores[i])
                         if bm25_scores[i] > 0])
        if self.embedder is not None:
            try:
                q = self.embedder.query(query)
                sims = [_cosine(q, v) for v in self.vectors]
                rankings.append(sorted(range(len(self.sections)), key=lambda i: -sims[i]))
            except Exception as exc:
                log.warning("query embedding failed, using BM25 only: %s", exc)
        fused: dict[int, float] = {}
        for ranking in rankings:
            for rank, index in enumerate(ranking):
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
        best = sorted(fused, key=lambda i: -fused[i])[:k]
        return [self.sections[i] for i in best]
