"""Team documents that extend the store manual: the web app's "Base de conhecimento" screen.

Ingestion: file -> text -> sections -> review -> index.
- Formats: Markdown, plain text and PDF (pypdf, the library that already reads the manual).
- Sections follow the document's own structure: Markdown headings, or numbered headings like the
  manual's ("2." / "2.1"). Without either, paragraphs are grouped up to about 1,200 characters.
  A section is the unit the agent receives and cites, so it should hold one idea.
- Review before use. Retrieved text reaches the model as store policy, which makes ingestion an
  entry point for prompt injection. Text addressed to the assistant ("ignore as instruções") and
  personal data (e-mail, phone, CPF) are flagged, and a flagged document is stored inactive until
  a person reads the warning and turns it on.
- The challenge's manual stays the fixed base. Evals build their own index from the manual alone,
  so team documents never move the reported numbers.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import chain
from pathlib import PurePath

from pypdf import PdfReader

from .memory import Database
from .policies import PolicySection, numbered_sections
from .privacy import EMAIL, PHONE
from .text import normalize

MAX_BYTES = 2_000_000
MAX_SECTIONS = 200
MIN_CHARS = 40
TARGET_CHARS = 1200  # paragraphs are grouped up to this size
MAX_SECTION_CHARS = 1800  # a longer section is split at paragraph breaks
MAX_TITLE = 80
STORE_DOMAIN, STORE_PHONE = "@emporiodamusica.com.br", "6733414444"  # public contacts, never flagged

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    filename   TEXT,
    kind       TEXT NOT NULL,          -- markdown | text | pdf
    content    TEXT NOT NULL,          -- extracted text; sections are derived from it on load
    digest     TEXT NOT NULL UNIQUE,   -- the same text is refused twice
    sections   INTEGER NOT NULL,
    warnings   TEXT NOT NULL,          -- JSON list from review()
    active     INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

MARKDOWN_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
# Text that speaks to the assistant instead of describing the store. Matched on normalized text
# (lowercase, no accents, no punctuation), so "Ignore as instruções!" and "ignore as instrucoes" match.
INSTRUCTIONS = [
    re.compile(r"\b(ignore|ignorar|desconsidere|desconsiderar|esqueca|esquecer)\b.{0,60}\b(instruc|regra|orientac|prompt)"),
    re.compile(r"\b(voce e|voce agora e|aja como|atue como|finja ser|finja que)\b.{0,40}"
               r"\b(assistente|ia|modelo|chatbot|robo|bot)\b"),
    re.compile(r"\b(prompt|instrucoes) (do|de) sistema\b|\bsystem prompt\b"),
    re.compile(r"\b(nao (conte|diga|informe|revele) (isso |isto )?(ao|aos|para o|para os) clientes?"
               r"|sempre responda|responda sempre)\b"),
]


class DuplicateDocument(ValueError):
    pass


@dataclass
class Extracted:
    text: str
    kind: str  # markdown | text | pdf
    pages: int | None = None


@dataclass
class Split:
    sections: list[PolicySection]
    method: str  # markdown | numbered | paragraphs


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _kind_of(text: str, suffix: str = "") -> str:
    if suffix in (".md", ".markdown") or any(MARKDOWN_HEADING.match(line) for line in text.splitlines()):
        return "markdown"
    return "text"


def from_text(text: str) -> Extracted:
    """Text pasted in the web app."""
    return Extracted(_tidy(text), _kind_of(text))


def extract_text(filename: str, data: bytes) -> Extracted:
    if len(data) > MAX_BYTES:
        size = f"{len(data) / 1e6:.1f}".replace(".", ",")
        raise ValueError(f"O arquivo tem {size} MB; o limite é 2 MB.")
    suffix = PurePath(filename or "").suffix.lower()
    if data[:5] == b"%PDF-":
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - any parser failure is a bad file for the user
            raise ValueError("Não consegui ler este PDF. Ele pode estar corrompido ou protegido por senha.") from exc
        text = "\n\n".join(pages)
        if not text.strip():
            raise ValueError("O PDF não tem texto selecionável (pode ser uma imagem escaneada). Envie a versão em texto.")
        return Extracted(_tidy(text), "pdf", len(pages))
    if suffix not in ("", ".md", ".markdown", ".txt"):
        raise ValueError("Formato não suportado. Envie Markdown (.md), texto (.txt) ou PDF.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # files saved by older Windows editors
    if "\x00" in text:
        raise ValueError("Formato não suportado. Envie Markdown (.md), texto (.txt) ou PDF.")
    return Extracted(_tidy(text), _kind_of(text, suffix))


def clean_title(title: str, filename: str | None = None, text: str = "") -> str:
    title = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f#*_`]", " ", title or "")).strip()
    if not title:  # a Markdown "# Title" says it better than a file name
        title = next((m.group(2) for line in text.splitlines()
                      if (m := MARKDOWN_HEADING.match(line)) and len(m.group(1)) == 1), "")
    if not title and filename:
        title = re.sub(r"[_-]+", " ", PurePath(filename).stem).strip().capitalize()
    if not title:
        first = next((line for line in text.splitlines() if line.strip()), "")
        title = re.sub(r"^#+\s*", "", first).strip()
    return title[:MAX_TITLE].strip() or "Documento sem título"


def _paragraph_chunks(text: str, limit: int = TARGET_CHARS) -> list[str]:
    """Groups paragraphs up to `limit` characters; a paragraph longer than that is cut at sentences."""
    pieces: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        sentence_chunk = ""
        for sentence in re.split(r"(?<=[.!?;])\s+", paragraph):
            if sentence_chunk and len(sentence_chunk) + len(sentence) + 1 > limit:
                pieces.append(sentence_chunk)
                sentence_chunk = sentence
            else:
                sentence_chunk = f"{sentence_chunk} {sentence}".strip()
        pieces.append(sentence_chunk)
    chunks: list[str] = []
    for piece in filter(None, pieces):
        if chunks and len(chunks[-1]) + len(piece) + 2 <= limit:
            chunks[-1] = f"{chunks[-1]}\n\n{piece}"
        else:
            chunks.append(piece)
    return chunks


def _numbered_parts(sections: list[PolicySection]) -> list[PolicySection]:
    """Long sections are split in parts named "(parte 1 de 2)", so each still fits a tool result."""
    result = []
    for section in sections:
        if len(section.body) <= MAX_SECTION_CHARS:
            result.append(section)
            continue
        chunks = _paragraph_chunks(section.body)
        result += [replace(section, title=f"{section.title} (parte {i} de {len(chunks)})", body=chunk)
                   for i, chunk in enumerate(chunks, 1)]
    return result


def _join_path(*parts: str) -> str:
    path: list[str] = []
    for part in filter(None, parts):
        if not path or normalize(path[-1]) != normalize(part):  # "# Title" repeating the document title
            path.append(part)
    return " > ".join(path)


def _markdown_sections(title: str, lines: list[str]) -> list[PolicySection]:
    sections: list[PolicySection] = []
    stack: list[tuple[int, str]] = []  # open headings: (level, text)
    body: list[str] = []
    for line in [*lines, "# end"]:  # the sentinel flushes the last section
        match = MARKDOWN_HEADING.match(line)
        if not match:
            body.append(line)
            continue
        text = "\n".join(body).strip()
        if text:
            name = stack[-1][1] if stack else "Introdução"
            parent = _join_path(title, *(t for _, t in stack[:-1]))
            if normalize(parent) == normalize(name):  # "# <document title>" followed by an intro
                parent = ""
            sections.append(PolicySection("", name, parent, text, title))
        body = []
        level = len(match.group(1))
        stack = [(lvl, txt) for lvl, txt in stack if lvl < level] + [(level, match.group(2).strip())]
    return sections


def split_document(title: str, text: str) -> Split:
    lines = text.split("\n")
    if any(MARKDOWN_HEADING.match(line) for line in lines):
        return Split(_numbered_parts(_markdown_sections(title, lines)), "markdown")
    numbered = numbered_sections(lines)
    if len(numbered) >= 2:
        sections = [replace(s, parent=_join_path(title, s.parent), source=title) for s in numbered]
        return Split(_numbered_parts(sections), "numbered")
    chunks = _paragraph_chunks(text)
    named = (lambda i: f"{title} (parte {i} de {len(chunks)})") if len(chunks) > 1 else (lambda i: title)
    return Split([PolicySection("", named(i), "", chunk, title) for i, chunk in enumerate(chunks, 1)], "paragraphs")


def _store_contact(value: str) -> bool:
    return value.lower().endswith(STORE_DOMAIN) or re.sub(r"\D", "", value).endswith(STORE_PHONE)


def _sentence_with(text: str, needle: str, limit: int = 160) -> str:
    """The original sentence (accents and case intact) holding a match found on normalized text."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if needle in normalize(sentence):
            sentence = sentence.strip()
            return sentence if len(sentence) <= limit else sentence[:limit].rsplit(" ", 1)[0] + "…"
    return needle


def _mask(value: str) -> str:
    return value if len(value) <= 6 else f"{value[:3]}…{value[-3:]}"


def review(title: str, sections: list[PolicySection]) -> list[dict]:
    """Warnings a person must read before the agent may use the document."""
    warnings = []
    for where, text in [("Título", title)] + [(s.label, s.body) for s in sections]:
        plain = normalize(text)
        for pattern in INSTRUCTIONS:
            if match := pattern.search(plain):
                warnings.append({"kind": "instruction", "section": where,
                                 "excerpt": _sentence_with(text, match.group(0))})
                break
        found = [m.group(0) for m in chain(EMAIL.finditer(text), PHONE.finditer(text), CPF.finditer(text))]
        found = [f for f in dict.fromkeys(found) if not _store_contact(f)]
        if found:
            warnings.append({"kind": "personal_data", "section": where,
                             "excerpt": ", ".join(_mask(f) for f in found[:3])})
    return warnings


class KnowledgeBase:
    """Team documents in SQLite. Sections are derived from the stored text when the index is built,
    so a better splitter applies to old documents too."""

    def __init__(self, db: Database):
        self.db = db
        with db.transaction() as conn:
            conn.executescript(SCHEMA)

    def add(self, title: str, extracted: Extracted, filename: str | None = None) -> tuple[dict, Split]:
        title = clean_title(title, filename, extracted.text)
        if len(extracted.text) < MIN_CHARS:
            raise ValueError("O documento tem pouco texto para indexar.")
        split = split_document(title, extracted.text)
        if not split.sections:
            raise ValueError("Não encontrei texto para indexar neste documento.")
        if len(split.sections) > MAX_SECTIONS:
            raise ValueError(f"O documento gerou {len(split.sections)} trechos; o limite é {MAX_SECTIONS}. "
                             "Divida-o em arquivos menores.")
        warnings = review(title, split.sections)
        digest = hashlib.sha256(extracted.text.encode()).hexdigest()
        existing = self.db.query("SELECT title FROM knowledge_documents WHERE digest = ?", (digest,))
        if existing:
            raise DuplicateDocument(f"Este texto já está na base, no documento “{existing[0]['title']}”.")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO knowledge_documents (title, filename, kind, content, digest, sections, warnings,"
                " active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, filename, extracted.kind, extracted.text, digest, len(split.sections),
                 json.dumps(warnings, ensure_ascii=False), int(not warnings),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return self.document(cursor.lastrowid), split

    @staticmethod
    def _row(row) -> dict:
        return {
            "id": row["id"], "title": row["title"], "filename": row["filename"], "kind": row["kind"],
            "sections": row["sections"], "chars": row["chars"], "active": bool(row["active"]),
            "warnings": json.loads(row["warnings"]), "created_at": row["created_at"],
        }

    def documents(self) -> list[dict]:
        rows = self.db.query("SELECT *, length(content) AS chars FROM knowledge_documents ORDER BY id DESC")
        return [self._row(row) for row in rows]

    def document(self, doc_id: int, with_sections: bool = False) -> dict | None:
        rows = self.db.query("SELECT *, length(content) AS chars FROM knowledge_documents WHERE id = ?", (doc_id,))
        if not rows:
            return None
        document = self._row(rows[0])
        if with_sections:
            document["section_list"] = split_document(rows[0]["title"], rows[0]["content"]).sections
        return document

    def set_active(self, doc_id: int, active: bool) -> dict | None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE knowledge_documents SET active = ? WHERE id = ?", (int(active), doc_id))
        return self.document(doc_id)

    def remove(self, doc_id: int) -> bool:
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (doc_id,)).rowcount > 0

    def active(self) -> list[tuple[str, str]]:
        return [(row["title"], row["content"]) for row in
                self.db.query("SELECT title, content FROM knowledge_documents WHERE active = 1 ORDER BY id")]

    def active_sections(self) -> list[PolicySection]:
        return [section for title, content in self.active() for section in split_document(title, content).sections]
