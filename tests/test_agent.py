"""Agent loop with a scripted fake model: no LLM server needed."""
import json
from types import SimpleNamespace

from emporio.agent import Agent
from emporio.config import load_settings
from emporio.tools import ToolCall


class FakeToolCall(SimpleNamespace):
    def model_dump(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


def tool_message(name, arguments, call_id="call_1"):
    call = FakeToolCall(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
    return SimpleNamespace(content="", tool_calls=[call])


def text_message(text):
    return SimpleNamespace(content=text, tool_calls=None)


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        message = self.script.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


class FakeTools:
    schemas = [{"type": "function", "function": {"name": "detalhes_produto"}}]
    session_id = None

    def run(self, name, arguments):
        return ToolCall(name, json.loads(arguments), {"preco_final": "R$ 2.199,00"}, 1.0)


def test_tool_result_goes_back_to_the_model_before_the_answer():
    client = FakeClient([tool_message("detalhes_produto", {"produto": "Takamine GD20"}),
                         text_message("O Takamine GD20 custa R$ 2.199,00.")])
    reply = Agent(load_settings(), client, FakeTools()).respond([], "Quanto custa o Takamine GD20?")
    assert reply.text == "O Takamine GD20 custa R$ 2.199,00."
    assert [c.name for c in reply.tool_calls] == ["detalhes_produto"]
    second = client.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and "2.199" in second[-1]["content"]
    # Static system prompt first (cacheable), volatile context right before the user message.
    assert second[0]["role"] == "system" and "Contexto" not in second[0]["content"]
    assert client.requests[0]["messages"][-2]["content"].startswith("Contexto: agora é")


def test_tool_call_written_as_text_is_still_executed():
    raw = '<tool_call>{"name": "detalhes_produto", "arguments": {"produto": "GD20"}}</tool_call>'
    client = FakeClient([text_message(raw), text_message("Custa R$ 2.199,00.")])
    reply = Agent(load_settings(), client, FakeTools()).respond([], "preço do GD20?")
    assert reply.tool_calls and reply.text == "Custa R$ 2.199,00."


def test_step_budget_forces_a_final_answer_without_tools(monkeypatch):
    monkeypatch.setenv("MAX_TOOL_STEPS", "2")
    loop = [tool_message("detalhes_produto", {"produto": "x"}, f"c{i}") for i in range(2)]
    client = FakeClient([*loop, text_message("Resposta final.")])
    reply = Agent(load_settings(), client, FakeTools()).respond([], "?")
    assert reply.text == "Resposta final."
    assert "tools" not in client.requests[-1]


# --------------------------------------------------------------------------- defences
from emporio.agent import calls_from_text, ungrounded_facts

TOOLS = {"consultar_pedido", "detalhes_produto"}


def test_broken_tool_call_text_is_recovered():
    # Seen with qwen2.5-7b in LM Studio: missing opening tag and a stray token.
    content = 'Ronaldo\n{"name": "consultar_pedido", "arguments": {"numero_pedido": 8, "email_ou_telefone": "a@b.com"}}\n</tool_call>'
    calls = calls_from_text(content, TOOLS)
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"numero_pedido": 8, "email_ou_telefone": "a@b.com"}
    assert calls_from_text('{"name": "rm_rf", "arguments": {}}', TOOLS) == []


def test_ungrounded_prices_and_tracking_codes_are_detected():
    evidence = '{"preco_final": "R$ 2.199,00", "pix": "R$ 2.089,05"} cliente: até R$1000'
    assert ungrounded_facts("Custa R$ 2.199,00 ou R$ 2.089,05 no PIX.", evidence) == []
    assert ungrounded_facts("Temos opções até R$ 1.000.", evidence) == []
    assert ungrounded_facts("Custa R$ 1.999,00.", evidence) == ["R$ 1999,00"]
    assert ungrounded_facts("Seu rastreio é BR4567890.", evidence) == ["BR4567890"]


def test_invented_price_triggers_one_grounded_retry():
    client = FakeClient([
        text_message("O Takamine GD20 custa R$ 1.999,00."),  # no tool, invented
        tool_message("detalhes_produto", {"produto": "Takamine GD20"}),
        text_message("O Takamine GD20 custa R$ 2.199,00."),
    ])
    reply = Agent(load_settings(), client, FakeTools()).respond([], "Quanto custa o Takamine GD20?")
    assert reply.grounding_retries == 1 and reply.ungrounded == []
    assert reply.text == "O Takamine GD20 custa R$ 2.199,00."
    warning = client.requests[1]["messages"][-1]
    assert warning["role"] == "system" and "R$ 1999,00" in warning["content"]


def test_invented_percentages_and_deadlines_are_detected():
    evidence = '"PAC (Correios) | 5 a 12 dias úteis" "devolução em até 7 (sete) dias" "18% de desconto"'
    assert ungrounded_facts("Pelo PAC leva de 5 a 12 dias úteis; arrependimento em 7 dias.", evidence) == []
    assert ungrounded_facts("Tem 18% de desconto.", evidence) == []
    assert ungrounded_facts("Chega em 2 a 3 dias úteis com 10% off.", evidence) == ["10%", "2 a 3 dias"]


def test_handoff_protocol_is_always_in_the_answer():
    class HandoffTools(FakeTools):
        def run(self, name, arguments):
            return ToolCall(name, json.loads(arguments), {"protocolo": "EM-20261103-0001"}, 1.0)

    client = FakeClient([tool_message("encaminhar_para_equipe", {"motivo": "reclamacao", "resumo": "risco"}),
                         text_message("Sua reclamação foi encaminhada.")])
    reply = Agent(load_settings(), client, HandoffTools()).respond([], "Quero reclamar")
    assert "EM-20261103-0001" in reply.text


def test_promotional_price_without_original_is_flagged():
    from emporio.agent import promotion_issues

    evidence = '"promocao": "20% de desconto (Desconto Exclusivo): de R$ 549,00 por R$ 439,20"'
    assert promotion_issues("Está em promoção por R$ 439,20!", evidence)
    assert promotion_issues("De R$ 549,00 por R$ 439,20 (20% de desconto).", evidence) == []
    assert promotion_issues("Temos outros ukuleles.", evidence) == []
