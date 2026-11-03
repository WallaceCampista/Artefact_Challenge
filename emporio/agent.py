"""The agent loop: plain function calling over the OpenAI chat completions API.

No framework on purpose: the loop is short, every step is visible, and it behaves the same
on any OpenAI-compatible server. Per user message:

    static system prompt + recent history + context (date/time) + message
      -> model answers with text, or with tool calls
      -> tools run in Python, results go back as "tool" messages
      -> repeat, at most `max_tool_steps` times, then force a text answer
      -> grounding check: every price / tracking code in the answer must come from a tool
         result or from the conversation; otherwise code looks up the products the answer names,
         and only if the answer is still unsupported is the model told so and answers again, once.
         A price still without a source after that is not sent: the customer gets a safe answer
      -> privacy guard on the final text (personal data of other customers, system prompt)

Every step is also emitted as a trace event (`on_event`), which the web UI streams live and
stores with the answer, so each reply can be audited: what was asked, what came back, which
guard fired.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import openai

from .catalog import CATEGORY_ALIASES, KNOWN_BRANDS
from .config import Settings
from .privacy import PrivacyGuard
from .prompts import SYSTEM_PROMPT, context_message
from .text import normalize, tokens
from .tools import ToolBox, ToolCall

# Small local models sometimes write the call as text (sometimes with a broken <tool_call> tag)
# instead of using the tool_calls field. Our tools take flat arguments, so [^{}] is enough.
CALL_AS_TEXT = re.compile(r'"name"\s*:\s*"(?P<name>[a-z_]+)"\s*,\s*"arguments"\s*:\s*(?P<args>\{[^{}]*\})', re.DOTALL)
MONEY = re.compile(r"R\$\s?(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")
TRACKING_LIKE = re.compile(r"\bBR[A-Z0-9]{6,}\b")
PERCENT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s?%")
INSTALLMENTS = re.compile(r"\b(\d{1,2})\s?x\b", re.IGNORECASE)
PLACEHOLDER = re.compile(r"\s*\[(?:nome|nome do cliente|cliente|seu nome)\]", re.IGNORECASE)
PROMO = re.compile(r"(\d+)% de desconto \([^)]*\): de (R\$ [\d.,]+) por (R\$ [\d.,]+)")
DAYS = re.compile(r"\b(\d+)(?:\s*(?:a|e|-)\s*(\d+))?\s+dias\b")
# An unconditional yes at the start of an answer ("Claro que pode!"), after an optional greeting.
UNVERIFIED_YES = re.compile(
    r"^(?P<greeting>\s*(?:ol[aá]|oi)\b[^!.\n]{0,25}?[!,.]\s*)?"
    r"(?:claro que (?:pode|sim)|sim,? (?:pode|claro)|pode sim|com certeza|sim(?=[!.]))[^,.!?\n]{0,25}?[,.!]\s*",
    re.IGNORECASE)
ASKS_FOR_ORDER = re.compile(r"n[uú]mero do (?:seu )?pedido", re.IGNORECASE)
GROUNDING_RETRIES = 1
PRICE_LOOKUPS = 3  # products looked up by code when an answer quotes a price without a source

FALLBACK_MESSAGE = (
    "Desculpe, estou com uma instabilidade no sistema agora. Tente de novo em instantes ou fale "
    "com a nossa equipe pelo (67) 3341-4444."
)
# Sent instead of an answer whose price or installment count has no source even after the rewrite.
# Tested live: asked to fix invented prices without a lookup, the model invented other prices.
UNVERIFIED_PRICE_MESSAGE = (
    "Não consegui confirmar esse valor no nosso sistema e prefiro não te passar uma informação errada. "
    "Pode me dizer a marca e o modelo do produto, ou reformular a pergunta, para eu consultar de novo? "
    "Se preferir, encaminho sua dúvida para a nossa equipe."
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
    redacted: list[str] = field(default_factory=list)  # personal data removed by the privacy guard
    trace: list[dict] = field(default_factory=list)  # every step of this turn, in order
    message_id: int | None = None  # set when the answer is persisted
    listener: Callable[[dict], None] | None = field(default=None, repr=False)
    started: float = field(default_factory=time.perf_counter, repr=False)

    def emit(self, kind: str, **data) -> None:
        event = {"type": kind, "ms": round((time.perf_counter() - self.started) * 1000), **data}
        self.trace.append(event)
        if self.listener is not None:
            self.listener(event)


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
    known_installments = set(INSTALLMENTS.findall(evidence))
    missing += [f"{n}x" for n in INSTALLMENTS.findall(answer) if n not in known_installments]
    missing += [code for code in TRACKING_LIKE.findall(answer) if code not in evidence]
    return missing


DAY_WORDS = ("depois de amanha", "amanha", "hoje", "agora", "domingo", "segunda", "terca", "quarta", "quinta",
             "sexta", "sabado")
VISIT_INTENT = re.compile(r"\b(?:abert\w*|abre|abrem|abrir|fecha\w*|funciona\w*|horario\w*|passar|visitar|"
                          r"ir (?:a|na|ate|ai|la))\b")
PAYMENT_INTENT = re.compile(r"\b(?:pix|parcel\w*|vezes|cartao|boleto|a vista|pagar|pagando|pagamento)\b")
DATE = re.compile(r"\b\d{1,2}/\d{1,2}\b")


def hours_question(message: str) -> str | None:
    """The day the customer asks about when the message is about visiting/opening on a day
    ("posso passar amanhã?", "vocês abrem sábado?", "abertos agora?"), else None."""
    text = normalize(message)
    if not VISIT_INTENT.search(text):
        return None
    date_match = DATE.search(message)
    if date_match:
        return date_match.group(0)
    for word in DAY_WORDS:
        if re.search(rf"\b{word}\b", text):
            return "hoje" if word == "agora" else word
    return None


# "Vocês têm X?" about a category. Questions with a model name, a price, a policy, an order or an
# accessory are left to the model: its own routing passes the evals there.
CATALOG_INTENT = re.compile(r"\b(?:tem|tens|teem|vende\w*|trabalh\w*|quais|opcoes|list\w*|mostr\w*|tiver)\b")
LISTING_INTENT = re.compile(r"\b(?:list\w*|mostr\w*|tiver|todos|todas|opcoes)\b")
NOT_A_CATALOG_QUESTION = re.compile(
    r"\b(?:pedido|compr\w*|chegou|devolv\w*|troc\w*|reclam\w*|garantia|frete|entreg\w*|prazo\w*|pix|parcel\w*|"
    r"pag\w*|horario\w*|abert\w*|endereco|quanto|preco\w*|valor\w*|custa\w*|barat\w*|car[oa]s?|"
    r"cordas?|palhetas?|cabos?|cases?|capas?|pedal|pedais|amplificador\w*|afinador\w*|encordoamento\w*|"
    r"estantes?|correias?|baquetas?|acessorios?)\b")
PROMOTION_WORDS = {"promocao", "promocoes", "oferta", "ofertas", "desconto", "descontos", "liquidacao"}
CATALOG_FILLER = {"ai", "aqui", "la", "algum", "alguma", "alguns", "algumas", "ainda", "disponivel", "disponiveis",
                  "estoque", "venda", "nao", "orcamento", "instrumento", "instrumentos", "tipo", "tipos", "modelo",
                  "modelos", "opcao", "opcoes", "todos", "todas", "hoje", "agora", "entao", "ok", "sim"}
ALIAS_WORDS = {word for aliases in CATEGORY_ALIASES.values() for alias in aliases for word in alias.split()}


def _category_question(text: str, store) -> dict | None:
    norm = normalize(text)
    if re.search(r"\d", norm) or not CATALOG_INTENT.search(norm) or NOT_A_CATALOG_QUESTION.search(norm):
        return None
    if store.products_mentioned(text):  # a model name: detalhes_produto territory
        return None
    padded = f" {norm} "
    categories = {cid for cid, aliases in CATEGORY_ALIASES.items() if any(f" {a} " in padded for a in aliases)}
    if len(categories) != 1:
        return None
    rest = [w for w in tokens(norm) if w not in ALIAS_WORDS and w not in CATALOG_FILLER and not CATALOG_INTENT.fullmatch(w)]
    promotions = any(w in PROMOTION_WORDS for w in rest)
    rest = [w for w in rest if w not in PROMOTION_WORDS]
    brands = [b for b in KNOWN_BRANDS if f" {normalize(b)} " in padded]
    if brands:
        term = brands[0]
    elif len(rest) <= 1:
        term = rest[0] if rest else None  # "baixo da Cort" -> "cort", a brand the store may not sell
    else:
        return None  # several unknown words: a description the model reads better than this rule
    arguments = {"categoria": store.categories[categories.pop()].name}
    if term:
        arguments["termo"] = term
    if promotions:
        arguments["apenas_promocoes"] = True
    return arguments


def catalog_search(message: str, previous: str | None, store) -> dict | None:
    """buscar_produtos arguments when the customer asks what the store has in a category, else None:
    "Baixo da Cort, vocês têm?" -> {"categoria": "Baixos", "termo": "cort"}. "Liste o que tiver" right
    after it repeats the previous question. Tested live, the model asked for a budget twice and then
    listed three Cort basses with prices; the store sells none."""
    found = _category_question(message, store)
    norm = normalize(message)
    if found is None and previous and LISTING_INTENT.search(norm) and not re.search(r"\d", norm) \
            and not NOT_A_CATALOG_QUESTION.search(norm):
        found = _category_question(previous, store)
    return found


def _is_price(fact: str) -> bool:
    """A money amount ("R$ 358,25") or an installment count ("12x") from ungrounded_facts."""
    return fact.startswith("R$") or fact.endswith("x")


def _tool_hint(missing: list[str], answer: str = "") -> str:
    hints = []
    if any(m.startswith("R$") for m in missing):
        hints.append("preços: detalhes_produto ou buscar_produtos")
        if re.search(r"parcel|vezes|\d+\s?x\b", normalize(answer)):
            # Live: the model divided the PIX price by 10 twice; card installments use the list price.
            hints.append("valor de cada parcela: copie de parcelas_sem_juros (detalhes_produto), que é calculado "
                         "sobre o preço de tabela; o desconto do PIX não vale no cartão")
    if any(m.endswith("x") for m in missing):
        hints.append("parcelas: detalhes_produto")
    if any(m.endswith(("%", "dias")) for m in missing):
        hints.append("prazos, percentuais e regras: consultar_politicas")
    if any(m.startswith("BR") for m in missing):
        hints.append("rastreio: consultar_pedido")
    return "consulte " + "; ".join(hints)


def drop_unverified_yes(answer: str) -> tuple[str, str | None]:
    """Removes a yes given before the order is checked: "Claro que pode! Me passe o número do pedido…"
    becomes "Me passe o número do pedido…". The answer asking for the order number is the sign that
    nothing was verified yet; whether a return is possible depends on deadlines the tool computes.
    Returns the answer and the removed words (None if nothing changed)."""
    match = UNVERIFIED_YES.match(answer) if ASKS_FOR_ORDER.search(answer) else None
    rest = answer[match.end():] if match else ""
    if not rest:
        return answer, None
    greeting = match.group("greeting") or ""
    return greeting + rest[0].upper() + rest[1:], answer[len(greeting):match.end()].strip()


def promotion_issues(answer: str, evidence: str) -> list[str]:
    """Policy 6.2: a promotional price must come with the original price and the discount."""
    issues = []
    for percent, original, final in set(PROMO.findall(evidence)):
        cited = _amounts(answer)
        if _amounts(final) <= cited and not (_amounts(original) <= cited and f"{percent}%" in answer):
            issues.append(f"o preço promocional {final} precisa vir com o preço original {original} e o desconto de {percent}% (regra 6.2)")
    return issues


def session_facts(calls) -> list[str]:
    """Facts established by tools in earlier turns of this session. Tool results are not replayed
    to the model, so without this it asks again for an order number it has already verified."""
    facts: dict[str, str] = {}
    for call in calls:
        name, args, result = ((call["name"], call["arguments"], call["result"]) if isinstance(call, dict)
                              else (call.name, call.arguments, call.result))
        if name == "consultar_pedido" and result.get("encontrado"):
            items = ", ".join(i["produto"] for i in result.get("itens", []))
            facts[f"order-{result['pedido']}"] = (
                f"pedido {result['pedido']} de {result.get('cliente')}, contato {args.get('email_ou_telefone')} "
                f"conferido ({items}; status {result.get('status')}). {result.get('conclusao_prazos', '')}".strip())
    return list(facts.values())


class Agent:
    def __init__(self, settings: Settings, client: openai.OpenAI, tools: ToolBox,
                 guard: PrivacyGuard | None = None):
        self.settings = settings
        self.client = client
        self.tools = tools
        self.guard = guard
        self.tool_names = {schema["function"]["name"] for schema in tools.schemas}

    def _complete(self, messages: list[dict], with_tools: bool, reply: Reply):
        kwargs = dict(model=self.settings.llm_model, messages=messages, temperature=self.settings.llm_temperature)
        if with_tools:
            kwargs["tools"] = self.tools.schemas
        reply.emit("model_call", n=reply.model_calls + 1, tools_enabled=with_tools)
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
                self._emit_tool(reply, executed, auto=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(executed.result, ensure_ascii=False),
                })
        # Step budget exhausted: answer with what was gathered, no more tools.
        return (self._complete(messages, with_tools=False, reply=reply).content or "").strip()

    @staticmethod
    def _emit_tool(reply: Reply, executed: ToolCall, auto: bool, reason: str | None = None) -> None:
        reply.emit("tool", name=executed.name, arguments=executed.arguments, result=executed.result,
                   duration_ms=round(executed.duration_ms, 1), auto=auto, reason=reason)

    def _prefetch(self, messages: list[dict], reply: Reply, name: str, arguments: dict, reason: str) -> None:
        """Runs a tool on the model's behalf and adds it to the conversation as if the model had
        called it. Used where routing is narrow and the cost of a wrong answer is high."""
        executed = self.tools.run(name, arguments)
        reply.tool_calls.append(executed)
        self._emit_tool(reply, executed, auto=True, reason=reason)
        call_id = f"auto-{len(reply.tool_calls)}"
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(executed.result, ensure_ascii=False)},
        ]

    def _product_in_focus(self, history: list[dict], message: str):
        """The single product named in this message or, failing that, in the latest user turns."""
        store = getattr(self.tools, "store", None)
        if store is None:
            return None
        recent = [m["content"] for m in reversed(history) if m["role"] == "user"][:3]
        for text in [message, *recent]:
            found = store.find_product(text)
            if found:
                return found[0] if len(found) == 1 else None
        return None

    def _look_up_products(self, messages: list[dict], reply: Reply, texts: list[str]) -> bool:
        """Runs detalhes_produto for the catalog products named in the first of `texts` that names
        any (the draft answer, then the message, then recent turns). True if something was looked up."""
        store = getattr(self.tools, "store", None)
        if store is None or "detalhes_produto" not in self.tool_names:
            return False
        known = {c.result.get("nome") for c in reply.tool_calls if c.name == "detalhes_produto"}
        for text in texts:
            products = [p for p in store.products_mentioned(text) if p.name not in known][:PRICE_LOOKUPS]
            if products:
                for product in products:
                    self._prefetch(messages, reply, "detalhes_produto", {"produto": product.name},
                                   reason="grounding: price without a source")
                return True
        return False

    def respond(self, history: list[dict], message: str, session_id: str | None = None,
                facts: list[str] | None = None, on_event: Callable[[dict], None] | None = None) -> Reply:
        """Answers one user message. `history` holds previous {"role", "content"} text turns;
        `facts` are what tools already established in this session (see session_facts);
        `on_event` receives each trace event as it happens."""
        started = time.perf_counter()
        self.tools.session_id = session_id
        reply = Reply(text="", listener=on_event, started=started)
        reply.emit("context", now=self.settings.now().isoformat(timespec="minutes"), facts=facts or [],
                   history_messages=len(history))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},  # static: cacheable prefix
            *history,
            # Volatile context as its own message. Tried inside the user turn: better at dates,
            # but the model then called fewer tools (evals 22/23 -> 18/23), so it stays here.
            {"role": "system", "content": context_message(self.settings.now()) + (
                "\nJá verificado nesta conversa (não peça esses dados de novo): " + " | ".join(facts) if facts else "")},
            {"role": "user", "content": message},
        ]
        # Evidence = the curated system prompt, the conversation (earlier answers were checked when they
        # were produced) and this turn's tool results.
        conversation_text = " ".join([SYSTEM_PROMPT, *(m["content"] for m in history), message])

        # Opening hours on a given day: a 7B model often ignores the calendar in the context and
        # answers from the weekly table (e.g. "come tomorrow 09-13" on a holiday). Asked in code.
        day = hours_question(message)
        if day and "consultar_horario" in self.tool_names:
            self._prefetch(messages, reply, "consultar_horario", {"quando": day},
                           reason="pre-routing: opening hours on a given day")
        # "Vocês têm baixo da Cort?": the prompt says to search before saying what the store has; the
        # model sometimes answers from memory instead, inventing models and prices. Searched in code.
        store = getattr(self.tools, "store", None)
        if store is not None and "buscar_produtos" in self.tool_names:
            previous = next((m["content"] for m in reversed(history) if m["role"] == "user"), None)
            search = catalog_search(message, previous, store)
            if search:
                self._prefetch(messages, reply, "buscar_produtos", search,
                               reason="pre-routing: what the store has in a category")
        # Payment of a product already discussed ("e no PIX, fica quanto?"): tool results are not
        # replayed between turns, so without a new lookup the model guesses installments.
        if PAYMENT_INTENT.search(normalize(message)):
            product = self._product_in_focus(history, message)
            if product is not None:
                self._prefetch(messages, reply, "detalhes_produto", {"produto": product.name},
                               reason="pre-routing: payment question about the product in focus")
        # Team documents (knowledge base) cover topics the prompt never mentions; tested live, the
        # model answered from the document's title in the tool description instead of calling the
        # tool. When the best section for the question comes from one of them, read it for the model.
        if self.tools.team_document_for(message):
            self._prefetch(messages, reply, "consultar_politicas", {"pergunta": message},
                           reason="pre-routing: question about a team document")

        text = self._loop(messages, reply)
        recent = [m["content"] for m in reversed(history)][:4]
        for attempt in range(GROUNDING_RETRIES + 1):
            results = " ".join(json.dumps(c.result, ensure_ascii=False) for c in reply.tool_calls)
            missing = ungrounded_facts(text, conversation_text + results)
            issues = promotion_issues(text, results)
            # A price without a source is often right but unlooked-up: tool results are not replayed
            # between turns, so "12x of the bass we discussed" has no evidence this turn. Look the
            # product up first and check the same answer again; a rewrite once turned a correct
            # R$ 358,25 into a wrong R$ 358,33.
            lookups: list[dict] = []
            if attempt < GROUNDING_RETRIES and any(_is_price(m) for m in missing) and \
                    self._look_up_products(lookups, reply, [text, message, *recent]):
                results = " ".join(json.dumps(c.result, ensure_ascii=False) for c in reply.tool_calls)
                missing = ungrounded_facts(text, conversation_text + results)
                issues = promotion_issues(text, results)
            if not (missing or issues) or attempt == GROUNDING_RETRIES:
                reply.ungrounded = missing
                outcome = "unresolved" if (missing or issues) else "pass"
                if any(_is_price(m) for m in missing):
                    # Decided in code: a price nobody can source never reaches the customer.
                    text, outcome = UNVERIFIED_PRICE_MESSAGE, "withheld"
                reply.emit("guard", check="grounding", outcome=outcome, details=missing + issues)
                break
            reply.grounding_retries += 1
            reply.emit("guard", check="grounding", outcome="retry", details=missing + issues)
            problems = issues + ([f"cita {', '.join(missing)}, que não aparece(m) em nenhum resultado de ferramenta "
                                  f"desta conversa ({_tool_hint(missing, text)})"] if missing else [])
            messages += [
                {"role": "assistant", "content": text},
                {"role": "system", "content": "Verificação automática da resposta acima: " + "; ".join(problems)
                 + ". Reescreva a resposta para o cliente corrigindo isso e usando só dados das ferramentas. "
                   "O cliente não viu a resposta acima: não peça desculpas por ela."},
                *lookups,
            ]
            # Invented deadlines/percentages almost always mean the manual was not read: read it for the model.
            used = {c.name for c in reply.tool_calls}
            if any(m.endswith(("%", "dias")) for m in missing) and "consultar_politicas" not in used:
                self._prefetch(messages, reply, "consultar_politicas", {"pergunta": message},
                               reason="grounding: deadline or percentage without a source")
            text = self._loop(messages, reply)

        text = PLACEHOLDER.sub("", text).strip() or "Desculpe, não consegui montar uma resposta agora. Pode reformular?"
        # The prompt says not to promise a return before checking the order; the model still opens with
        # "Claro que pode!" (stable across runs in the evals). Decided in code, like the protocol below.
        if "consultar_pedido" not in {c.name for c in reply.tool_calls}:
            text, promise = drop_unverified_yes(text)
            if promise:
                reply.emit("guard", check="promise", outcome="removed", details=[promise])
        # Critical information is guaranteed by code, not left to the model: the handoff protocol.
        for call in reply.tool_calls:
            protocol = call.result.get("protocolo") if call.name == "encaminhar_para_equipe" else None
            if protocol and protocol not in text:
                text += f"\n\nSeu protocolo de atendimento é {protocol}. Nossa equipe retorna em até 24 horas úteis."
                reply.emit("guard", check="protocol", outcome="appended", details=[protocol])
        if self.guard is not None:
            user_text = " ".join(m["content"] for m in history if m["role"] == "user") + " " + message
            checked = self.guard.apply(text, user_text)
            text, reply.redacted = checked.text, checked.redacted
            if checked.prompt_leak:
                reply.emit("guard", check="system_prompt", outcome="blocked", details=[])
            elif checked.redacted:
                reply.emit("guard", check="personal_data", outcome="redacted", details=checked.redacted)
        reply.text = text
        reply.latency_s = time.perf_counter() - started
        return reply
