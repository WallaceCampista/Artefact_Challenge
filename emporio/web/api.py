"""HTTP API and web UI: `python -m emporio serve`.

Thin layer over ChatService, so the web, the CLI and the evals run exactly the same agent.

    GET  /                          web app (chat, under-the-hood trace, team inbox, metrics)
    GET  /health                    liveness + whether the model server answers
    GET  /api/config                model names, policy search mode, demo clocks, whether team screens need a code
    POST /api/chat                  one turn, JSON answer
    POST /api/chat/stream           one turn as Server-Sent Events: trace events live, then the answer
    GET  /api/sessions/{id}         transcript with per-answer trace and metrics
    POST /api/feedback              thumbs up/down on an answer

Team screens (need the X-Team-Token header when TEAM_TOKEN is set):
    GET  /api/handoffs              tickets for the human team (?status=open|resolved)
    POST /api/handoffs/{p}/resolve  close a ticket
    GET  /api/metrics               operational metrics from the stored conversations
    GET  /api/knowledge             the index: store manual + team documents
    POST /api/knowledge             add a document (Markdown, text or PDF), reviewed and indexed
    GET  /api/knowledge/manual      the manual's sections, as the agent receives them
    GET  /api/knowledge/{id}        a team document and its sections
    PATCH/DELETE /api/knowledge/{id}  turn a document on or off, or remove it (re-indexes)
    POST /api/knowledge/search      which sections a question retrieves, with each ranking

The customer endpoints stay open: a session id is a random id held by the customer's browser.

Answers are streamed as progress events, not token by token: the guards check the complete
answer before the customer sees it (grounding, promotion rule, personal data), so streaming
raw tokens would show text that might be corrected or redacted a moment later.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..agent import FALLBACK_MESSAGE, ModelUnavailable, Reply
from ..app import AGENT_SECTIONS, POLICY_PDF, ChatService, parse_clock
from ..config import Settings
from ..knowledge import MAX_BYTES, DuplicateDocument
from ..policies import PolicySection

STATIC = Path(__file__).parent / "static"
SESSION_ID = r"^[A-Za-z0-9_-]{1,64}$"
DEMO_CLOCKS = [
    {"label": "Horário real", "value": None},
    {"label": "Ter 03/11, 10:30 (loja aberta)", "value": "2026-11-03T10:30:00"},
    {"label": "Dom 01/11, 10:00 (véspera de Finados)", "value": "2026-11-01T10:00:00"},
    {"label": "Sáb 07/11, 14:00 (loja já fechou)", "value": "2026-11-07T14:00:00"},
    {"label": "Qui 24/12, 17:30 (véspera de Natal)", "value": "2026-12-24T17:30:00"},
]


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
    message: str = Field(min_length=1, max_length=2000)
    simulated_now: str | None = Field(default=None, description="ISO date-time; empty = real clock")


class FeedbackRequest(BaseModel):
    message_id: int
    rating: str = Field(pattern=r"^(up|down)$")
    comment: str | None = Field(default=None, max_length=1000)


class DocumentUpload(BaseModel):
    title: str = Field(default="", max_length=200)
    filename: str | None = Field(default=None, max_length=200)
    data_base64: str | None = Field(default=None, max_length=MAX_BYTES * 4 // 3 + 4,
                                    description="the file's bytes (Markdown, text or PDF)")
    text: str | None = Field(default=None, max_length=500_000, description="pasted text, instead of a file")


class DocumentUpdate(BaseModel):
    active: bool


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=5, ge=1, le=10)


def section_json(section: PolicySection) -> dict:
    return {"number": section.number, "label": section.label, "parent": section.parent,
            "source": section.source, "body": section.body}


def serialize(reply: Reply) -> dict:
    return {
        "message_id": reply.message_id,
        "text": reply.text,
        "trace": reply.trace,
        "tools": [c.name for c in reply.tool_calls],
        "latency_ms": round(reply.latency_s * 1000),
        "model_calls": reply.model_calls,
        "tokens": reply.prompt_tokens + reply.completion_tokens,
        "grounding_retries": reply.grounding_retries,
        "ungrounded": reply.ungrounded,
        "redactions": len(reply.redacted),
    }


def create_app(settings: Settings | None = None, service: ChatService | None = None) -> FastAPI:
    if service is None:
        if settings is None:
            raise ValueError("pass settings or a ChatService")
        service = ChatService(settings)
    settings = service.settings
    app = FastAPI(title="Empório da Música — Tom", version="1.0",
                  description="Customer service agent API (see README).")
    app.state.service = service
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def team_only(x_team_token: str = Header(default="")) -> None:
        """Team screens show customers' contacts and change what the agent reads."""
        expected = settings.team_token
        if expected and not secrets.compare_digest(x_team_token.encode(), expected.encode()):
            raise HTTPException(401, "Área da equipe: informe o código de acesso.")

    team = [Depends(team_only)]

    def validated_clock(value: str | None) -> str | None:
        try:
            return parse_clock(value)
        except ValueError:
            raise HTTPException(422, "simulated_now must be an ISO date-time, e.g. 2026-11-01T10:00:00")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        try:
            service.agent.client.with_options(timeout=3).models.list()
            llm = "up"
        except Exception:  # noqa: BLE001 - any failure means "down" for a health check
            llm = "down"
        return {"status": "ok", "llm": llm, "model": settings.llm_model}

    @app.get("/api/config")
    def config():
        return {
            "model": settings.llm_model,
            "embedding_model": settings.embedding_model or None,
            "policy_search": service.policy_mode,
            "timezone": settings.timezone,
            "clocks": DEMO_CLOCKS,
            "team_auth": bool(settings.team_token),
        }

    @app.post("/api/chat")
    def chat(request: ChatRequest):
        clock = validated_clock(request.simulated_now)
        try:
            reply = service.send(request.session_id, request.message, simulated_now=clock)
        except ModelUnavailable:
            raise HTTPException(503, FALLBACK_MESSAGE)
        return serialize(reply)

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatRequest):
        clock = validated_clock(request.simulated_now)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: dict) -> None:  # called from the worker thread
            loop.call_soon_threadsafe(queue.put_nowait, ("trace", event))

        async def run() -> None:
            try:
                reply = await asyncio.to_thread(service.send, request.session_id, request.message,
                                                clock, on_event)
                await queue.put(("answer", serialize(reply)))
            except ModelUnavailable:
                await queue.put(("error", {"text": FALLBACK_MESSAGE}))
            except Exception as exc:  # noqa: BLE001 - reported to the client, never a hung stream
                await queue.put(("error", {"text": FALLBACK_MESSAGE, "detail": type(exc).__name__}))
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())

        async def events():
            while (item := await queue.get()) is not None:
                kind, data = item
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str):
        if not re.match(SESSION_ID, session_id):
            raise HTTPException(422, "invalid session id")
        return {"session_id": session_id, "messages": service.conversations.transcript(session_id)}

    @app.post("/api/feedback")
    def feedback(request: FeedbackRequest):
        if not service.feedback.rate(request.message_id, request.rating, request.comment):
            raise HTTPException(404, "answer not found")
        return {"ok": True}

    @app.get("/api/handoffs", dependencies=team)
    def handoffs(status: str | None = None):
        if status not in (None, "open", "resolved"):
            raise HTTPException(422, "status must be open or resolved")
        return {"handoffs": service.desk.list(status)}

    @app.post("/api/handoffs/{protocol}/resolve", dependencies=team)
    def resolve(protocol: str):
        if not service.desk.resolve(protocol):
            raise HTTPException(404, "no open ticket with this protocol")
        return {"ok": True}

    @app.get("/api/metrics", dependencies=team)
    def metrics():
        return service.analytics.summary()

    @app.get("/api/knowledge", dependencies=team)
    def knowledge():
        return {
            "index": service.index_summary(),
            "embedding_model": settings.embedding_model or None,
            "agent_sections": AGENT_SECTIONS,
            "manual": {"title": "Manual de políticas e procedimentos", "filename": POLICY_PDF,
                       "sections": len(service.manual)},
            "documents": service.knowledge.documents(),
        }

    @app.post("/api/knowledge", status_code=201, dependencies=team)
    def add_document(upload: DocumentUpload):
        if (upload.data_base64 is None) == (upload.text is None):
            raise HTTPException(422, "Envie um arquivo ou cole um texto.")
        data = None
        if upload.data_base64 is not None:
            try:
                data = base64.b64decode(upload.data_base64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(422, "O arquivo chegou corrompido. Tente enviar de novo.")
        try:
            return service.ingest(upload.title, upload.filename, data=data, text=upload.text)
        except DuplicateDocument as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/knowledge/manual", dependencies=team)
    def manual_sections():
        return {"sections": [section_json(s) for s in service.manual]}

    @app.get("/api/knowledge/{doc_id}", dependencies=team)
    def document(doc_id: int):
        found = service.knowledge.document(doc_id, with_sections=True)
        if found is None:
            raise HTTPException(404, "document not found")
        found["section_list"] = [section_json(s) for s in found["section_list"]]
        return found

    @app.patch("/api/knowledge/{doc_id}", dependencies=team)
    def update_document(doc_id: int, update: DocumentUpdate):
        result = service.set_document_active(doc_id, update.active)
        if result is None:
            raise HTTPException(404, "document not found")
        return result

    @app.delete("/api/knowledge/{doc_id}", dependencies=team)
    def delete_document(doc_id: int):
        result = service.remove_document(doc_id)
        if result is None:
            raise HTTPException(404, "document not found")
        return result

    @app.post("/api/knowledge/search", dependencies=team)
    def search(request: SearchRequest):
        hits = service.search_policies(request.query, request.k)
        return {
            "mode": service.policy_mode,
            "agent_sections": AGENT_SECTIONS,
            "hits": [{**section_json(h.section), "score": h.score, "bm25_rank": h.bm25_rank,
                      "bm25_score": h.bm25_score, "vector_rank": h.vector_rank, "similarity": h.similarity}
                     for h in hits],
        }

    return app
