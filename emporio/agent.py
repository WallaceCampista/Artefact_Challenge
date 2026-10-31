"""The agent loop: plain function calling over the OpenAI chat completions API.

No framework on purpose: the loop is short, every step is visible, and it behaves the same
on any OpenAI-compatible server. Per user message:

    system prompt + recent history + message
      -> model answers with text (done) or with tool calls
      -> tools run in Python, results go back as "tool" messages
      -> repeat, at most `max_tool_steps` times, then force a text answer
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

import openai

from .config import Settings
from .prompts import SYSTEM_PROMPT, context_message
from .tools import ToolBox, ToolCall

# Some local models occasionally write the call as text instead of using the tool_calls field.
TOOL_CALL_IN_TEXT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

FALLBACK_MESSAGE = (
    "Desculpe, estou com uma instabilidade no sistema agora. Tente de novo em instantes ou fale "
    "com a nossa equipe pelo (67) 3341-4444."
)


class ModelUnavailable(RuntimeError):
    """The LLM server could not be reached or returned an error."""


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model_calls: int = 0
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _calls_from_text(content: str) -> list[dict]:
    calls = []
    for raw in TOOL_CALL_IN_TEXT.findall(content or ""):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "name" in data:
            arguments = data.get("arguments", {})
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": data["name"], "arguments": json.dumps(arguments, ensure_ascii=False)},
            })
    return calls


class Agent:
    def __init__(self, settings: Settings, client: openai.OpenAI, tools: ToolBox):
        self.settings = settings
        self.client = client
        self.tools = tools

    def _complete(self, messages: list[dict], with_tools: bool, reply: Reply):
        kwargs = dict(model=self.settings.llm_model, messages=messages, temperature=self.settings.llm_temperature)
        if with_tools:
            kwargs["tools"] = self.tools.schemas
        try:
            response = self.client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            raise ModelUnavailable(str(exc)) from exc
        reply.model_calls += 1
        if response.usage:
            reply.prompt_tokens += response.usage.prompt_tokens or 0
            reply.completion_tokens += response.usage.completion_tokens or 0
        return response.choices[0].message

    def respond(self, history: list[dict], message: str, session_id: str | None = None) -> Reply:
        """Answers one user message. `history` holds previous {"role", "content"} text turns."""
        started = time.perf_counter()
        self.tools.session_id = session_id
        reply = Reply(text="")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},  # static: cacheable prefix
            *history,
            {"role": "system", "content": context_message(self.settings.now())},
            {"role": "user", "content": message},
        ]

        for _ in range(self.settings.max_tool_steps):
            answer = self._complete(messages, with_tools=True, reply=reply)
            calls = [c.model_dump() for c in answer.tool_calls] if answer.tool_calls else _calls_from_text(answer.content)
            if not calls:
                reply.text = (answer.content or "").strip()
                break
            messages.append({"role": "assistant", "content": answer.content or "", "tool_calls": calls})
            for call in calls:
                executed = self.tools.run(call["function"]["name"], call["function"]["arguments"])
                reply.tool_calls.append(executed)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(executed.result, ensure_ascii=False),
                })
        else:
            # Step budget exhausted: answer with what was gathered, no more tools.
            answer = self._complete(messages, with_tools=False, reply=reply)
            reply.text = (answer.content or "").strip()

        if not reply.text:
            reply.text = "Desculpe, não consegui montar uma resposta agora. Pode reformular a pergunta?"
        reply.latency_s = time.perf_counter() - started
        return reply
