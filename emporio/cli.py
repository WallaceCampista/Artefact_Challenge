"""Interactive terminal chat: `python -m emporio chat`."""
from __future__ import annotations

import json
import textwrap

from .agent import FALLBACK_MESSAGE, ModelUnavailable, Reply
from .app import build_agent
from .config import Settings

BANNER = """Empório da Música — atendimento virtual (Tom)
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


def run_chat(settings: Settings, session: str | None, debug: bool) -> None:
    agent = build_agent(settings)
    history: list[dict] = []
    print(BANNER)
    while True:
        try:
            message = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not message:
            continue
        if message in ("/quit", "/sair"):
            return
        if message in ("/new", "/nova"):
            history = []
            print("(new conversation)\n")
            continue
        try:
            reply = agent.respond(history[-2 * settings.history_turns:], message, session_id=session)
        except ModelUnavailable as exc:
            if debug:
                print(f"  · model error: {exc}")
            print(f"Tom: {FALLBACK_MESSAGE}\n")
            continue
        if debug:
            print_trace(reply)
        print(f"Tom: {reply.text}\n")
        history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply.text}]
