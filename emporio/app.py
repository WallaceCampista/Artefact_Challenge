"""Wires the pieces together. Used by the CLI, the evals and the example recorder."""
from __future__ import annotations

import logging

from .agent import Agent
from .catalog import Store
from .config import Settings
from .llm import Embedder, make_client
from .policies import PolicyIndex
from .tools import HandoffDesk, ToolBox

POLICY_PDF = "políticas_da_loja.pdf"


def build_agent(settings: Settings, desk: HandoffDesk | None = None) -> Agent:
    client = make_client(settings)
    store = Store.load(settings.data_dir)
    embedder = Embedder(client, settings.embedding_model) if settings.embedding_model else None
    policies = PolicyIndex.from_pdf(settings.data_dir / POLICY_PDF, embedder, cache_dir=settings.var_dir)
    logging.getLogger(__name__).info("policy search: %s", policies.mode)
    tools = ToolBox(store, policies, desk or HandoffDesk(), clock=settings.now)
    return Agent(settings, client, tools)
