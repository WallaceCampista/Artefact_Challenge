"""Wires the pieces together. Used by the CLI, the evals and the example recorder."""
from __future__ import annotations

import logging

from .agent import Agent, Reply
from .catalog import Store
from .config import Settings
from .llm import Embedder, make_client
from .memory import ConversationStore, Database, SqliteHandoffDesk
from .policies import PolicyIndex
from .tools import HandoffDesk, ToolBox

POLICY_PDF = "políticas_da_loja.pdf"
log = logging.getLogger(__name__)


def build_agent(settings: Settings, desk=None) -> Agent:
    client = make_client(settings)
    store = Store.load(settings.data_dir)
    embedder = Embedder(client, settings.embedding_model) if settings.embedding_model else None
    policies = PolicyIndex.from_pdf(settings.data_dir / POLICY_PDF, embedder, cache_dir=settings.var_dir)
    log.info("policy search: %s", policies.mode)
    tools = ToolBox(store, policies, desk or HandoffDesk(), clock=settings.now)
    return Agent(settings, client, tools)


class ChatService:
    """One entry point per channel (CLI, HTTP): load history, answer, persist."""

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.conversations = ConversationStore(self.db)
        self.desk = SqliteHandoffDesk(self.db)
        self.agent = build_agent(settings, self.desk)

    @property
    def policy_mode(self) -> str:
        return self.agent.tools.policies.mode

    def send(self, session_id: str, text: str) -> Reply:
        history = self.conversations.history(session_id, self.settings.history_turns)
        reply = self.agent.respond(history, text, session_id=session_id)
        self.conversations.save_exchange(session_id, text, reply.text, reply.tool_calls)
        return reply
