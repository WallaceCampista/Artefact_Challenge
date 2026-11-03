"""Interactive terminal chat: `python -m emporio chat [--session ID] [--debug]`."""
from __future__ import annotations

import json
import textwrap
import uuid

from .agent import FALLBACK_MESSAGE, ModelUnavailable, Reply
from .app import ChatService
from .config import Settings

BANNER = """Empório da Música — atendimento virtual (Tom)
Model: {model} · policy search: {policy}
Session: {session}{resumed}
Commands: /new starts a new conversation, /quit exits.
"""


def print_trace(reply: Reply) -> None:
    for call in reply.tool_calls:
        args = json.dumps(call.arguments, ensure_ascii=False)
        result = json.dumps(call.result, ensure_ascii=False)
        print(f"  · {call.name}({args}) [{call.duration_ms:.0f} ms]")
        print(textwrap.indent(textwrap.shorten(result, 300, placeholder=" …"), "      "))
    print(f"  · {reply.model_calls} model call(s), {reply.prompt_tokens}+{reply.completion_tokens} tokens, "
          f"{reply.latency_s:.1f} s")


def new_session_id() -> str:
    return f"cli-{uuid.uuid4().hex[:8]}"


def run_chat(settings: Settings, session: str | None, debug: bool) -> None:
    service = ChatService(settings)
    session = session or new_session_id()
    previous = service.conversations.count(session)
    resumed = f" (resumed, {previous} messages)" if previous else ""
    print(BANNER.format(model=settings.llm_model, policy=service.policy_mode, session=session, resumed=resumed))
    for message in service.conversations.history(session, 1):
        speaker = "Você" if message["role"] == "user" else "Tom"
        print(f"{speaker}: {message['content']}\n")

    while True:
        try:
            text = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text in ("/quit", "/sair"):
            return
        if text in ("/new", "/nova"):
            session = new_session_id()
            print(f"(new conversation: {session})\n")
            continue
        try:
            reply = service.send(session, text)
        except ModelUnavailable as exc:
            if debug:
                print(f"  · model error: {exc}")
            print(f"Tom: {FALLBACK_MESSAGE}\n")
            continue
        if debug:
            print_trace(reply)
        print(f"Tom: {reply.text}\n")


def print_handoffs(settings: Settings) -> None:
    from .memory import Database, SqliteHandoffDesk

    tickets = SqliteHandoffDesk(Database(settings.db_path)).list_open()
    if not tickets:
        print("No open handoffs.")
    for t in tickets:
        print(f"{t['protocol']}  [{t['reason']}]  session={t['session_id']}  contact={t['contact'] or '-'}")
        print(f"    {t['summary']}")


def print_negative_feedback(settings: Settings, as_cases: bool = False) -> None:
    """Answers customers rated down: each one is a candidate for a new eval case. With as_cases,
    prints them as draft cases for evals/cases.json."""
    from .evals import draft_case
    from .memory import Database, FeedbackStore

    items = FeedbackStore(Database(settings.db_path)).negatives()
    if as_cases:
        print(json.dumps([draft_case(item, settings.timezone) for item in items], ensure_ascii=False, indent=1))
        return
    if not items:
        print("No negative feedback.")
    for item in items:
        print(f"[{item['session_id']} #{item['id']}] Q: {item['question']}")
        print(f"    A: {textwrap.shorten(item['answer'], 200, placeholder=' …')}")
        if item["comment"]:
            print(f"    comment: {item['comment']}")
