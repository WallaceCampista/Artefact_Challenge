"""Wires the pieces together. Used by the CLI, the web API, the evals and the example recorder."""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime

from .agent import Agent, Reply, session_facts
from .catalog import Store
from .config import Settings
from .knowledge import KnowledgeBase, extract_text, from_text
from .llm import Embedder, make_client
from .memory import Analytics, ConversationStore, Database, FeedbackStore, SqliteHandoffDesk
from .policies import PolicyIndex, parse_sections
from .privacy import PrivacyGuard
from .prompts import SYSTEM_PROMPT
from .tools import HandoffDesk, ToolBox

POLICY_PDF = "políticas_da_loja.pdf"
MAX_CLOCKS = 8  # agents kept for simulated clocks (web demo); the oldest is dropped first
AGENT_SECTIONS = 3  # sections consultar_politicas hands to the model
log = logging.getLogger(__name__)


def make_embedder(settings: Settings, client=None) -> Embedder | None:
    if not settings.embedding_model:
        return None
    return Embedder(client or make_client(settings), settings.embedding_model)


def build_agent(settings: Settings, desk=None, policies: PolicyIndex | None = None) -> Agent:
    """Without `policies`, the index holds the store manual alone (evals, example recordings)."""
    client = make_client(settings)
    store = Store.load(settings.data_dir)
    if policies is None:
        policies = PolicyIndex.from_pdf(settings.data_dir / POLICY_PDF, make_embedder(settings, client),
                                        cache_dir=settings.var_dir)
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
        self.knowledge = KnowledgeBase(self.db)
        self.manual = parse_sections(settings.data_dir / POLICY_PDF)
        self.policies = self._build_index()
        self.agent = self._attach(build_agent(settings, self.desk, self.policies))
        self._clocks: OrderedDict[str, Agent] = OrderedDict()
        self._lock = threading.Lock()
        self._index_lock = threading.Lock()

    @property
    def policy_mode(self) -> str:
        return self.policies.mode

    # ---- knowledge base: the store manual plus the team's active documents ----
    def _build_index(self) -> PolicyIndex:
        return PolicyIndex(self.manual + self.knowledge.active_sections(), make_embedder(self.settings),
                           cache_dir=self.settings.var_dir)

    def _attach(self, agent: Agent) -> Agent:
        agent.tools.use_policies(self.policies, [title for title, _ in self.knowledge.active()])
        return agent

    def reindex(self) -> PolicyIndex:
        """Rebuilds the index after a document changed. Only new sections are embedded (vectors are
        cached by text), and the swap does not wait for an answer in progress."""
        with self._index_lock:
            self.policies = self._build_index()
            for agent in (self.agent, *list(self._clocks.values())):
                self._attach(agent)
        return self.policies

    def index_summary(self, index: PolicyIndex | None = None) -> dict:
        index = index or self.policies
        report = index.embeddings
        return {"sections": len(index.sections), "manual": len(self.manual),
                "team": len(index.sections) - len(self.manual), "mode": index.mode,
                "embedded": report.embedded, "cached": report.cached, "ms": report.ms, "error": report.error}

    def ingest(self, title: str, filename: str | None = None, data: bytes | None = None,
               text: str | None = None) -> dict:
        """Reads, splits, reviews and stores a document; indexes it now unless review flagged it."""
        started = time.perf_counter()
        extracted = extract_text(filename or "", data) if data is not None else from_text(text or "")
        read_ms = round((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        document, split = self.knowledge.add(title, extracted, filename)
        split_ms = round((time.perf_counter() - started) * 1000)
        return {
            "document": document,
            "read": {"kind": extracted.kind, "pages": extracted.pages, "chars": len(extracted.text), "ms": read_ms},
            "split": {"method": split.method, "sections": len(split.sections), "ms": split_ms},
            "review": {"warnings": document["warnings"]},
            "index": self.index_summary(self.reindex()) if document["active"] else None,
        }

    def set_document_active(self, doc_id: int, active: bool) -> dict | None:
        document = self.knowledge.set_active(doc_id, active)
        return document and {"document": document, "index": self.index_summary(self.reindex())}

    def remove_document(self, doc_id: int) -> dict | None:
        document = self.knowledge.document(doc_id)
        if document is None or not self.knowledge.remove(doc_id):
            return None
        return {"document": document, "index": self.index_summary(self.reindex() if document["active"] else None)}

    def search_policies(self, query: str, k: int = 5) -> list:
        return self.policies.explain(query, k)

    def agent_for(self, simulated_now: str | None) -> Agent:
        """The agent answering at a simulated date and time (demo: holidays, closed hours)."""
        if not simulated_now:
            return self.agent
        if simulated_now not in self._clocks:
            settings = dataclasses.replace(self.settings, reference_datetime=simulated_now)
            self._clocks[simulated_now] = self._attach(build_agent(settings, self.desk, self.policies))
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
