"""Wires the pieces together. Used by the CLI, the web API, the evals and the example recorder."""
from __future__ import annotations

import dataclasses
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime

from .agent import Agent, Reply, session_facts
from .catalog import Store
from .config import Settings
from .llm import Embedder, make_client
from .memory import Analytics, ConversationStore, Database, FeedbackStore, SqliteHandoffDesk
from .policies import PolicyIndex
from .privacy import PrivacyGuard
from .prompts import SYSTEM_PROMPT
from .tools import HandoffDesk, ToolBox

POLICY_PDF = "políticas_da_loja.pdf"
MAX_CLOCKS = 8  # agents kept for simulated clocks (web demo); the oldest is dropped first
log = logging.getLogger(__name__)


def build_agent(settings: Settings, desk=None) -> Agent:
    client = make_client(settings)
    store = Store.load(settings.data_dir)
    embedder = Embedder(client, settings.embedding_model) if settings.embedding_model else None
    policies = PolicyIndex.from_pdf(settings.data_dir / POLICY_PDF, embedder, cache_dir=settings.var_dir)
    log.info("policy search: %s", policies.mode)
    tools = ToolBox(store, policies, desk or HandoffDesk(), clock=settings.now)
    return Agent(settings, client, tools, guard=PrivacyGuard(store.customers.values(), SYSTEM_PROMPT))


def parse_clock(value: str | None) -> str | None:
    """Validates a simulated clock ("2026-11-01T10:00"); None means the real clock."""
    if not value:
        return None
    datetime.fromisoformat(value)  # raises ValueError when invalid
    return value


class ChatService:
    """One entry point per channel (CLI, HTTP): load history, answer, persist.

    The local model serves one request at a time (LM Studio --parallel 1), so turns are
    serialized with a lock; a production deployment would put a queue in front instead.
    """

    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.conversations = ConversationStore(self.db)
        self.feedback = FeedbackStore(self.db)
        self.analytics = Analytics(self.db)
        self.desk = SqliteHandoffDesk(self.db)
        self.agent = build_agent(settings, self.desk)
        self._clocks: OrderedDict[str, Agent] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def policy_mode(self) -> str:
        return self.agent.tools.policies.mode

    def agent_for(self, simulated_now: str | None) -> Agent:
        """The agent answering at a simulated date and time (demo: holidays, closed hours)."""
        if not simulated_now:
            return self.agent
        if simulated_now not in self._clocks:
            settings = dataclasses.replace(self.settings, reference_datetime=simulated_now)
            self._clocks[simulated_now] = build_agent(settings, self.desk)
            if len(self._clocks) > MAX_CLOCKS:
                self._clocks.popitem(last=False)
        self._clocks.move_to_end(simulated_now)
        return self._clocks[simulated_now]

    def send(self, session_id: str, text: str, simulated_now: str | None = None,
             on_event: Callable[[dict], None] | None = None) -> Reply:
        simulated_now = parse_clock(simulated_now)
        turns = self.settings.history_turns
        with self._lock:
            agent = self.agent_for(simulated_now)
            history = self.conversations.history(session_id, turns)
            facts = session_facts(self.conversations.tool_trail(session_id, turns))
            reply = agent.respond(history, text, session_id=session_id, facts=facts, on_event=on_event)
            reply.message_id = self.conversations.save_exchange(session_id, text, reply.text, reply.tool_calls, {
                "trace": reply.trace,
                "latency_ms": round(reply.latency_s * 1000),
                "model_calls": reply.model_calls,
                "prompt_tokens": reply.prompt_tokens,
                "completion_tokens": reply.completion_tokens,
                "grounding_retries": reply.grounding_retries,
                "redactions": len(reply.redacted),
                "simulated_now": simulated_now,
            })
        return reply
