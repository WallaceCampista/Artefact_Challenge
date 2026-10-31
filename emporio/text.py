"""Portuguese text normalization shared by the catalog search and the policy retriever."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

# Short Portuguese stopword list: enough to keep questions like "qual o preço do ..."
# from matching every document on "o" and "do".
STOPWORDS = frozenset(
    """
    a o as os um uma uns umas de do da dos das no na nos nas em por para pra pro com sem
    e ou que qual quais quanto quanta quantos quantas como onde quando se ao aos me te
    meu minha meus minhas seu sua seus suas voce voces vcs eu nos ele ela eles elas isso
    isto esse essa este esta aquele aquela tem ter tenho ha sao ser esta estao estou
    vou vai pode posso podem gostaria queria quero saber oi ola bom boa dia tarde noite
    ate mais muito muita entre sobre favor obrigado obrigada tudo bem loja
    """.split()
)

STEM_LENGTH = 5  # prefix stemming: "devolver" and "devolução" both become "devol"


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lowercase, no accents, hyphenated model names glued ("D-28" -> "d28")."""
    text = strip_accents(text).lower()
    text = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def tokens(text: str, *, keep_stopwords: bool = False) -> list[str]:
    words = normalize(text).split()
    return words if keep_stopwords else [w for w in words if w not in STOPWORDS]


def stem(token: str) -> str:
    """Truncation stemmer. Crude, but a solid baseline for Portuguese retrieval."""
    return token if token.isdigit() else token[:STEM_LENGTH]


def stems(text: str) -> list[str]:
    return [stem(t) for t in tokens(text)]


class BM25:
    """Okapi BM25 over pre-tokenized documents (k1 = 1.5, b = 0.75)."""

    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if documents else 0.0
        df = Counter(term for doc in self.docs for term in doc)
        n = len(documents)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        result = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term in set(query):
                tf = doc.get(term, 0)
                if not tf:
                    continue
                norm = self.k1 * (1 - self.b + self.b * length / (self.avg_length or 1))
                score += self.idf[term] * tf * (self.k1 + 1) / (tf + norm)
            result.append(score)
        return result


def format_brl(value: float) -> str:
    """1234.5 -> 'R$ 1.234,50'."""
    formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"
