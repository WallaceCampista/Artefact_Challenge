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
