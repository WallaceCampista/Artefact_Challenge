"""Thin wrapper over any OpenAI-compatible server (LM Studio, OpenAI, Gemini, Groq...)."""
from __future__ import annotations

from openai import OpenAI

from .config import Settings

QUERY_TASK = "Given a customer question to a music store, retrieve the store policy section that answers it"


def make_client(settings: Settings) -> OpenAI:
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "not-needed",
        timeout=settings.llm_timeout,
        max_retries=1,
    )


class Embedder:
    """Embeddings for the policy retriever, with each model family's input format.

    - Qwen3-Embedding: queries carry a task instruction, documents go as they are.
    - nomic-embed-text: "search_query: " / "search_document: " prefixes.
    - Anything else (e.g. OpenAI text-embedding-3-*): plain text.
    """

    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model
        self._family = next((f for f in ("qwen3", "nomic") if f in model.lower()), "plain")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    def documents(self, texts: list[str]) -> list[list[float]]:
        if self._family == "nomic":
            texts = [f"search_document: {t}" for t in texts]
        return self._embed(texts)

    def query(self, text: str) -> list[float]:
        if self._family == "qwen3":
            text = f"Instruct: {QUERY_TASK}\nQuery: {text}"
        elif self._family == "nomic":
            text = f"search_query: {text}"
        return self._embed([text])[0]
