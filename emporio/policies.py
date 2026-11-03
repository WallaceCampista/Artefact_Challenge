"""Retrieval over the store's policy manual (`data/políticas_da_loja.pdf`).

Pipeline: PDF -> clean text -> one chunk per numbered section -> hybrid search.
- Chunking by section: the manual is short (8 pages) and already organised in numbered
  sections, so a section is the natural unit of meaning and gives a citable source ("4.1").
- Hybrid search: BM25 catches exact terms ("PIX", "SEDEX", "boleto"); embeddings catch
  paraphrases ("quanto tempo demora pra chegar" -> delivery deadlines). Both rankings are
  merged with Reciprocal Rank Fusion, which needs no score calibration between them.
- Without an embedding model (or if the server fails), search falls back to BM25 alone.
- Embeddings are cached per section text, so adding a team document (see knowledge.py) only
  embeds that document's sections.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from .text import BM25, stems

log = logging.getLogger(__name__)

HEADER = "Empório da Música Manual de Políticas e Procedimentos"
HEADING = re.compile(r"^(\d+)(?:\.(\d+))?\.?\s+(\S.*)$")
RRF_K = 60
EMBED_BATCH = 32


@dataclass(frozen=True)
class PolicySection:
    number: str  # "4.1"
    title: str  # "Direito de Arrependimento (Compras Online)"
    parent: str  # "4. Política de Trocas e Devoluções" ("" for top-level sections)
    body: str
    source: str = ""  # title of the team document it came from; "" for the store manual

    @property
    def label(self) -> str:
        return f"{self.number} {self.title}" if self.number else self.title

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
    return numbered_sections(_join_wrapped(lines))


def numbered_sections(lines: list[str]) -> list[PolicySection]:
    """Splits text at numbered headings ("4. Trocas", "4.1 Arrependimento"); text before the first
    heading is dropped. Also used for team documents numbered the same way."""
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


@dataclass
class Hit:
    """One retrieved section and why: its place in each ranking and the fused score."""
    section: PolicySection
    score: float  # Reciprocal Rank Fusion
    bm25_rank: int | None = None  # 1-based; None when no query term occurs in the section
    bm25_score: float = 0.0
    vector_rank: int | None = None  # None without embeddings
    similarity: float | None = None


@dataclass
class EmbeddingReport:
    embedded: int = 0  # sections sent to the embedding model while building this index
    cached: int = 0  # sections whose vectors were already on disk
    ms: int = 0
    error: str | None = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class PolicyIndex:
    def __init__(self, sections: list[PolicySection], embedder=None, cache_dir: Path | None = None):
        self.sections = sections
        # Titles count twice: they are the best summary of what a section is about.
        self.bm25 = BM25([stems(f"{s.title} {s.title} {s.parent} {s.body}") for s in sections])
        self.embedder = None
        self.vectors: list[list[float]] = []
        self.embeddings = EmbeddingReport()
        if embedder is not None:
            started = time.perf_counter()
            try:
                self.vectors = self._load_vectors(embedder, cache_dir)
                self.embedder = embedder
            except Exception as exc:  # server down, model not loaded...
                log.warning("embeddings unavailable, policy search uses BM25 only: %s", exc)
                self.embeddings.error = type(exc).__name__
            self.embeddings.ms = round((time.perf_counter() - started) * 1000)

    @classmethod
    def from_pdf(cls, pdf_path: Path, embedder=None, cache_dir: Path | None = None) -> "PolicyIndex":
        return cls(parse_sections(pdf_path), embedder, cache_dir)

    @property
    def mode(self) -> str:
        return "hybrid (BM25 + embeddings, RRF)" if self.embedder else "BM25"

    def _load_vectors(self, embedder, cache_dir: Path | None) -> list[list[float]]:
        """One vector per section text, cached on disk by text: re-indexing after a team document
        changes only embeds the sections that are new."""
        texts = [s.text for s in self.sections]
        keys = [_digest(t) for t in texts]
        cache = cache_dir / f"embeddings-{_digest(embedder.model)}.json" if cache_dir else None
        stored: dict[str, list[float]] = {}
        if cache and cache.exists():
            try:
                stored = json.loads(cache.read_text())
            except ValueError:
                log.warning("embedding cache %s is unreadable, rebuilding it", cache.name)
        missing = list(dict.fromkeys(t for t, key in zip(texts, keys) if key not in stored))
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start:start + EMBED_BATCH]
            stored.update(zip((_digest(t) for t in batch), embedder.documents(batch)))
        if missing and cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            partial = cache.with_suffix(f".{os.getpid()}.tmp")
            partial.write_text(json.dumps(stored))
            os.replace(partial, cache)  # atomic: the web app and an eval run may share var/
        self.embeddings.embedded, self.embeddings.cached = len(missing), len(texts) - len(missing)
        return [stored[key] for key in keys]

    def explain(self, query: str, k: int = 3) -> list[Hit]:
        """The k best sections with each ranking that put them there (the web app shows this)."""
        n = len(self.sections)
        bm25_scores = self.bm25.scores(stems(query))
        bm25_order = [i for i in sorted(range(n), key=lambda i: -bm25_scores[i]) if bm25_scores[i] > 0]
        rankings = [bm25_order]
        sims: list[float] = []
        if self.embedder is not None:
            try:
                q = self.embedder.query(query)
                sims = [_cosine(q, v) for v in self.vectors]
                rankings.append(sorted(range(n), key=lambda i: -sims[i]))
            except Exception as exc:
                log.warning("query embedding failed, using BM25 only: %s", exc)
        fused: dict[int, float] = {}
        places: list[dict[int, int]] = [{index: rank for rank, index in enumerate(r)} for r in rankings]
        for ranking in rankings:
            for rank, index in enumerate(ranking):
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
        hits = []
        for i in sorted(fused, key=lambda i: -fused[i])[:k]:
            hit = Hit(self.sections[i], round(fused[i], 4), bm25_score=round(bm25_scores[i], 2))
            if i in places[0]:
                hit.bm25_rank = places[0][i] + 1
            if len(places) > 1:
                hit.vector_rank, hit.similarity = places[1][i] + 1, round(sims[i], 3)
            hits.append(hit)
        return hits

    def search(self, query: str, k: int = 3) -> list[PolicySection]:
        return [hit.section for hit in self.explain(query, k)]
