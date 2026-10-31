"""The agent loop: plain function calling over the OpenAI chat completions API.

No framework on purpose: the loop is short, every step is visible, and it behaves the same
on any OpenAI-compatible server. Per user message:

    static system prompt + recent history + context (date/time) + message
      -> model answers with text, or with tool calls
      -> tools run in Python, results go back as "tool" messages
      -> repeat, at most `max_tool_steps` times, then force a text answer
      -> grounding check: every price / tracking code in the answer must come from a tool
         result or from the conversation; otherwise the model is told so and answers again, once
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

# Small local models sometimes write the call as text (sometimes with a broken <tool_call> tag)
# instead of using the tool_calls field. Our tools take flat arguments, so [^{}] is enough.
CALL_AS_TEXT = re.compile(r'"name"\s*:\s*"(?P<name>[a-z_]+)"\s*,\s*"arguments"\s*:\s*(?P<args>\{[^{}]*\})', re.DOTALL)
MONEY = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")
TRACKING_LIKE = re.compile(r"\bBR[A-Z0-9]{6,}\b")
GROUNDING_RETRIES = 1

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
    grounding_retries: int = 0
    ungrounded: list[str] = field(default_factory=list)  # still unsupported after the retry


def calls_from_text(content: str | None, known_tools: set[str]) -> list[dict]:
    calls = []
    for match in CALL_AS_TEXT.finditer(content or ""):
        if match.group("name") not in known_tools:
            continue
        try:
            arguments = json.loads(match.group("args"))
        except json.JSONDecodeError:
            continue
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": match.group("name"), "arguments": json.dumps(arguments, ensure_ascii=False)},
        })
    return calls


def _amounts(text: str) -> set[float]:
    values = set()
    for raw in MONEY.findall(text):
        if "," in raw or raw.count(".") > 1 or re.search(r"\.\d{3}$", raw):
            raw = raw.replace(".", "").replace(",", ".")  # Brazilian format: 1.234,56
        try:
            values.add(round(float(raw), 2))
        except ValueError:
            pass
    return values


def ungrounded_facts(answer: str, evidence: str) -> list[str]:
    """Prices and tracking codes in the answer that do not appear in the evidence text."""
    known = _amounts(evidence)
    missing = [f"R$ {v:.2f}".replace(".", ",") for v in sorted(_amounts(answer))
               if not any(abs(v - k) < 0.011 for k in known)]
    missing += [code for code in TRACKING_LIKE.findall(answer) if code not in evidence]
    return missing


class Agent:
    def __init__(self, settings: Settings, client: openai.OpenAI, tools: ToolBox):
        self.settings = settings
        self.client = client
        self.tools = tools
        self.tool_names = {schema["function"]["name"] for schema in tools.schemas}

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

    def _loop(self, messages: list[dict], reply: Reply) -> str:
        """Model <-> tools until the model answers with text."""
        for _ in range(self.settings.max_tool_steps):
            answer = self._complete(messages, with_tools=True, reply=reply)
            if answer.tool_calls:
                calls = [c.model_dump() for c in answer.tool_calls]
            else:
                calls = calls_from_text(answer.content, self.tool_names)
            if not calls:
                return (answer.content or "").strip()
            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            for call in calls:
                executed = self.tools.run(call["function"]["name"], call["function"]["arguments"])
                reply.tool_calls.append(executed)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(executed.result, ensure_ascii=False),
                })
        # Step budget exhausted: answer with what was gathered, no more tools.
        return (self._complete(messages, with_tools=False, reply=reply).content or "").strip()

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
        # Earlier answers were already checked when they were produced, so they count as evidence.
        conversation_text = " ".join(m["content"] for m in history) + " " + message

        text = self._loop(messages, reply)
        for attempt in range(GROUNDING_RETRIES + 1):
            evidence = conversation_text + " ".join(json.dumps(c.result, ensure_ascii=False) for c in reply.tool_calls)
            missing = ungrounded_facts(text, evidence)
            if not missing or attempt == GROUNDING_RETRIES:
                reply.ungrounded = missing
                break
            reply.grounding_retries += 1
            messages += [
                {"role": "assistant", "content": text},
                {"role": "system", "content": (
                    f"Verificação automática: a resposta acima cita {', '.join(missing)}, que não aparece(m) em "
                    "nenhum resultado de ferramenta desta conversa. Consulte a ferramenta adequada e reescreva a "
                    "resposta para o cliente usando só dados retornados por ela.")},
            ]
            text = self._loop(messages, reply)

        reply.text = text or "Desculpe, não consegui montar uma resposta agora. Pode reformular a pergunta?"
        reply.latency_s = time.perf_counter() - started
        return reply
