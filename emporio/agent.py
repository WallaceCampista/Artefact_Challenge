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
PERCENT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s?%")
PROMO = re.compile(r"(\d+)% de desconto \([^)]*\): de (R\$ [\d.,]+) por (R\$ [\d.,]+)")
DAYS = re.compile(r"\b(\d+)(?:\s*(?:a|e|-)\s*(\d+))?\s+dias\b")
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


def _day_spans(text: str) -> set[tuple[str, ...]]:
    text = re.sub(r"\(\w+\)", "", text)  # "7 (sete) dias" -> "7 dias"
    return {tuple(n for n in match if n) for match in DAYS.findall(text)}


def ungrounded_facts(answer: str, evidence: str) -> list[str]:
    """Prices, percentages, day counts and tracking codes in the answer that are not in the evidence."""
    known = _amounts(evidence)
    missing = [f"R$ {v:.2f}".replace(".", ",") for v in sorted(_amounts(answer))
               if not any(abs(v - k) < 0.011 for k in known)]
    known_percents = {p.replace(",", ".") for p in PERCENT.findall(evidence)}
    missing += [f"{p}%" for p in PERCENT.findall(answer) if p.replace(",", ".") not in known_percents]
    known_spans = _day_spans(evidence)
    known_days = {n for span in known_spans for n in span}
    for span in _day_spans(answer):
        if span not in known_spans and not (len(span) == 1 and span[0] in known_days):
            missing.append(f"{' a '.join(span)} dias")
    missing += [code for code in TRACKING_LIKE.findall(answer) if code not in evidence]
    return missing


def _tool_hint(missing: list[str]) -> str:
    hints = []
    if any(m.startswith("R$") for m in missing):
        hints.append("preços: detalhes_produto ou buscar_produtos")
    if any(m.endswith(("%", "dias")) for m in missing):
        hints.append("prazos, percentuais e regras: consultar_politicas")
    if any(m.startswith("BR") for m in missing):
        hints.append("rastreio: consultar_pedido")
    return "consulte " + "; ".join(hints)


def promotion_issues(answer: str, evidence: str) -> list[str]:
    """Policy 6.2: a promotional price must come with the original price and the discount."""
    issues = []
    for percent, original, final in set(PROMO.findall(evidence)):
        cited = _amounts(answer)
        if _amounts(final) <= cited and not (_amounts(original) <= cited and f"{percent}%" in answer):
            issues.append(f"o preço promocional {final} precisa vir com o preço original {original} e o desconto de {percent}% (regra 6.2)")
    return issues


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
        # Evidence = the curated system prompt, the conversation (earlier answers were checked when they
        # were produced) and this turn's tool results.
        conversation_text = " ".join([SYSTEM_PROMPT, *(m["content"] for m in history), message])

        text = self._loop(messages, reply)
        for attempt in range(GROUNDING_RETRIES + 1):
            results = " ".join(json.dumps(c.result, ensure_ascii=False) for c in reply.tool_calls)
            missing = ungrounded_facts(text, conversation_text + results)
            issues = promotion_issues(text, results)
            if not (missing or issues) or attempt == GROUNDING_RETRIES:
                reply.ungrounded = missing
                break
            reply.grounding_retries += 1
            problems = issues + ([f"cita {', '.join(missing)}, que não aparece(m) em nenhum resultado de ferramenta "
                                  f"desta conversa ({_tool_hint(missing)})"] if missing else [])
            messages += [
                {"role": "assistant", "content": text},
                {"role": "system", "content": "Verificação automática da resposta acima: " + "; ".join(problems)
                 + ". Reescreva a resposta para o cliente corrigindo isso e usando só dados das ferramentas."},
            ]
            text = self._loop(messages, reply)

        text = text or "Desculpe, não consegui montar uma resposta agora. Pode reformular a pergunta?"
        # Critical information is guaranteed by code, not left to the model: the handoff protocol.
        for call in reply.tool_calls:
            protocol = call.result.get("protocolo") if call.name == "encaminhar_para_equipe" else None
            if protocol and protocol not in text:
                text += f"\n\nSeu protocolo de atendimento é {protocol}. Nossa equipe retorna em até 24 horas úteis."
        reply.text = text
        reply.latency_s = time.perf_counter() - started
        return reply
