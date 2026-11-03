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

    def team_document_for(self, question):
        return None  # no knowledge base documents in these tests


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
from emporio.agent import (UNVERIFIED_PRICE_MESSAGE, calls_from_text, catalog_search, drop_unverified_yes,
                           cut_other_script, tidy_closing, ungrounded_facts, verdict_issues)

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


def test_installments_must_be_grounded_and_placeholders_removed():
    assert ungrounded_facts("Dá pra parcelar em até 12x.", "até 12x de R$ 183,25") == []
    assert ungrounded_facts("Dá pra parcelar em até 10x.", "até 12x de R$ 183,25") == ["10x"]
    client = FakeClient([text_message("Olá [Nome], tudo bem?")])
    assert Agent(load_settings(), client, FakeTools()).respond([], "oi").text == "Olá, tudo bem?"


def test_hours_question_detects_visits_on_a_day():
    from emporio.agent import hours_question

    assert hours_question("Posso passar aí amanhã de manhã para conhecer os ukuleles?") == "amanha"
    assert hours_question("Vocês abrem sábado?") == "sabado"
    assert hours_question("Vocês estão abertos agora?") == "hoje"
    assert hours_question("A loja abre dia 25/12?") == "25/12"
    assert hours_question("Qual a previsão do tempo amanhã?") is None
    assert hours_question("Quanto custa o GD20?") is None


def test_hours_are_prefetched_before_the_model_answers():
    class HoursTools(FakeTools):
        schemas = FakeTools.schemas + [{"type": "function", "function": {"name": "consultar_horario"}}]

        def run(self, name, arguments):
            args = arguments if isinstance(arguments, dict) else json.loads(arguments)
            return ToolCall(name, args, {"situacao": "FECHADA (feriado: Finados)"}, 1.0)

    client = FakeClient([text_message("Amanhã estamos fechados (Finados).")])
    reply = Agent(load_settings(), client, HoursTools()).respond([], "Posso passar aí amanhã?")
    assert [c.name for c in reply.tool_calls] == ["consultar_horario"]
    assert "Finados" in client.requests[0]["messages"][-1]["content"]


def test_payment_follow_up_looks_the_product_up_again(store):
    class StoreTools(FakeTools):
        def __init__(self):
            self.store = store

        def run(self, name, arguments):
            args = arguments if isinstance(arguments, dict) else json.loads(arguments)
            return ToolCall(name, args, {"pix": "R$ 2.089,05", "parcelamento": "até 12x de R$ 183,25"}, 1.0)

    history = [{"role": "user", "content": "Quanto custa o Takamine GD20?"},
               {"role": "assistant", "content": "Custa R$ 2.199,00."}]
    client = FakeClient([text_message("No PIX fica R$ 2.089,05 e parcela em até 12x.")])
    reply = Agent(load_settings(), client, StoreTools()).respond(history, "E no PIX? Parcela em quantas vezes?")
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [
        ("detalhes_produto", {"produto": "Takamine GD20 Dreadnought Natural"})]
    assert reply.ungrounded == []



class CatalogTools(FakeTools):
    """Fake tools over the real catalog: detalhes_produto answers with the payment line."""

    def __init__(self, store, installment="até 12x de R$ 358,25 sem juros"):
        self.store, self.installment = store, installment

    def run(self, name, arguments):
        args = arguments if isinstance(arguments, dict) else json.loads(arguments)
        if name == "buscar_produtos":
            return ToolCall(name, args, {"total_encontrado": 0, "observacao": "Nenhum produto corresponde a 'cort'."}, 1.0)
        return ToolCall(name, args, {"nome": args["produto"], "pagamento": {"cartao_de_credito": self.installment}}, 1.0)


def test_a_price_without_a_source_is_looked_up_before_any_rewrite(store):
    # Live case: 12x of a product discussed turns earlier. The value was right but had no source this turn,
    # and the rewrite turned R$ 358,25 into a wrong R$ 358,33. Now code looks the product up and re-checks.
    history = [{"role": "user", "content": "Se eu parcelar em 12 vezes, quanto fica?"},
               {"role": "assistant", "content": "Você quer parcelar o Yamaha Bass 3X em 12 vezes?"}]
    draft = "Em 12x sem juros, o Yamaha Bass 3X fica R$ 358,25 por parcela."
    client, events = FakeClient([text_message(draft)]), []
    reply = Agent(load_settings(), client, CatalogTools(store)).respond(
        history, "Sim, foi exatamente o que eu pedi", on_event=events.append)
    assert reply.text == draft and len(client.requests) == 1 and reply.grounding_retries == 0
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [
        ("detalhes_produto", {"produto": "Yamaha Bass 3X Electric Bass"})]
    assert [e["reason"] for e in events if e["type"] == "tool"] == ["grounding: price without a source"]
    assert [e["outcome"] for e in events if e["type"] == "guard"] == ["pass"]


def test_a_price_still_without_a_source_after_the_rewrite_is_never_sent(store):
    # Live case: the store sells no Cort basses; the model listed invented ones, and the rewrite invented
    # other prices. Nothing in the catalog matches, so the customer gets a safe answer instead.
    client, events = FakeClient([text_message("Temos o Cort S-250HF por R$ 4.299,00."),
                                 text_message("O Cort S-250HF sai por R$ 3.799,00.")]), []
    reply = Agent(load_settings(), client, CatalogTools(store)).respond(
        [], "Não tenho orçamento, me lista os baixos da Cort", on_event=events.append)
    assert reply.text == UNVERIFIED_PRICE_MESSAGE and reply.ungrounded == ["R$ 3799,00"] and not reply.tool_calls
    assert [e["outcome"] for e in events if e["type"] == "guard"] == ["retry", "withheld"]
    assert "não peça desculpas" in client.requests[1]["messages"][-1]["content"]


def test_an_installment_rewrite_is_told_where_the_value_is_and_that_pix_does_not_apply():
    client = FakeClient([text_message("Em 10 vezes, cada parcela fica R$ 1.087,28."),
                         text_message("Em 10 vezes, cada parcela fica R$ 1.144,50.")])
    Agent(load_settings(), client, FakeTools()).respond([], "E em 10 vezes?")
    note = client.requests[1]["messages"][-1]["content"]
    assert "parcelas_sem_juros" in note and "PIX não vale no cartão" in note


def test_a_wrong_price_of_a_real_product_is_rewritten_with_its_details(store):
    client = FakeClient([text_message("Em 12x, o Yamaha Bass 3X fica R$ 358,33."),
                         text_message("Em 12x, o Yamaha Bass 3X fica R$ 358,25.")])
    reply = Agent(load_settings(), client, CatalogTools(store)).respond([], "E em 12x?")
    assert reply.text.endswith("R$ 358,25.") and reply.grounding_retries == 1 and reply.ungrounded == []
    retry = client.requests[1]["messages"]
    assert retry[-1]["role"] == "tool" and "358,25" in retry[-1]["content"]  # the lookup reached the rewrite


def test_a_yes_before_the_order_is_checked_is_removed():
    asks = "Para confirmar se está no prazo, preciso do número do seu pedido e do e-mail do cadastro."
    assert drop_unverified_yes("Claro que pode! " + asks) == (asks, "Claro que pode!")
    assert drop_unverified_yes("Olá, Letícia! Sim, pode sim. " + asks)[0] == "Olá, Letícia! " + asks
    assert drop_unverified_yes("Claro que pode, me passe o número do pedido.")[0] == "Me passe o número do pedido."
    assert drop_unverified_yes("Oi Letícia, claro que pode! " + asks)[0] == "Oi Letícia, " + asks
    # Not asking for the order: nothing to verify, nothing removed.
    assert drop_unverified_yes("Claro que pode passar na loja amanhã!") == ("Claro que pode passar na loja amanhã!", None)

    client, events = FakeClient([text_message("Claro que pode! " + asks)]), []
    reply = Agent(load_settings(), client, FakeTools()).respond(
        [], "Me arrependi da minha compra, posso devolver?", on_event=events.append)
    assert reply.text == asks
    assert [(e["check"], e["outcome"]) for e in events if e["type"] == "guard"][-1] == ("promise", "removed")


def test_questions_about_what_the_store_has_are_searched_in_code(store):
    assert catalog_search("quais baixos vocês têm?", None, store) == {"categoria": "Baixos"}
    assert catalog_search("Baixo da Cort, vocês têm?", None, store) == {"categoria": "Baixos", "termo": "cort"}
    # "List what you have" right after it repeats the previous question.
    assert catalog_search("não tenho orçamento, liste-me o que tiver", "Baixo da Cort, vocês têm?", store) == \
        {"categoria": "Baixos", "termo": "cort"}
    assert catalog_search("Tem algum violão em promoção?", None, store) == {"categoria": "Violões", "apenas_promocoes": True}
    # Left to the model: a model name, a price, a policy, an accessory, a long description.
    for message in ["Vocês têm o violão Shelby SN-7C?", "Quais violões até R$ 1000?", "Esse violão tem garantia?",
                    "Vocês vendem cordas de violão?", "Qual o violão de preço mais baixo?",
                    "Vocês têm violão bom para iniciante que caiba na mochila?"]:
        assert catalog_search(message, None, store) is None, message

    class SearchTools(CatalogTools):
        schemas = [{"type": "function", "function": {"name": n}} for n in ("buscar_produtos", "detalhes_produto")]

    client = FakeClient([text_message("Não trabalhamos com a Cort, mas temos outros baixos.")])
    reply = Agent(load_settings(), client, SearchTools(store)).respond([], "Baixo da Cort, vocês têm?")
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("buscar_produtos", {"categoria": "Baixos", "termo": "cort"})]
    assert client.requests[0]["messages"][-1]["role"] == "tool"  # the model answers with the search in front of it


ALL_ENDED = ("Hoje (03/11/2026) TODOS os prazos da loja já terminaram: não cabe arrependimento, troca nem garantia "
             "legal. Resta só a garantia do fabricante (8.2), que a loja pode intermediar.")


def test_an_answer_that_contradicts_the_deadline_verdict_is_rewritten():
    order = ToolCall("consultar_pedido", {}, {"conclusao_prazos": ALL_ENDED}, 1.0)
    # Live: the model wrote the opposite of the conclusion in front of it.
    assert verdict_issues("Seu pedido está dentro de todos os prazos legais para arrependimento.", [order])
    assert verdict_issues("Você ainda pode devolver o produto.", [order])
    assert not verdict_issues("Infelizmente, você não está mais dentro do prazo de arrependimento.", [order])
    assert not verdict_issues("Todos os prazos da loja já terminaram.", [order])
    still_open = ToolCall("consultar_pedido", {}, {"conclusao_prazos": "Hoje ainda estão abertos: 4.2. Os demais já terminaram."}, 1.0)
    assert not verdict_issues("A troca por defeito ainda está dentro do prazo.", [still_open])

    class OrderTools(FakeTools):
        schemas = [{"type": "function", "function": {"name": "consultar_pedido"}}]

        def run(self, name, arguments):
            return ToolCall(name, json.loads(arguments) if isinstance(arguments, str) else arguments,
                            {"conclusao_prazos": ALL_ENDED}, 1.0)

    client = FakeClient([tool_message("consultar_pedido", {"numero_pedido": 7, "email_ou_telefone": "a@b.com"}),
                         text_message("Seu pedido está dentro de todos os prazos para arrependimento."),
                         text_message("Todos os prazos da loja já terminaram; resta a garantia do fabricante.")])
    reply = Agent(load_settings(), client, OrderTools()).respond([], "É o pedido 7, e-mail a@b.com")
    assert reply.text.startswith("Todos os prazos") and reply.grounding_retries == 1
    assert ALL_ENDED in client.requests[2]["messages"][-1]["content"]


def test_the_closing_question_is_dropped_after_another_question():
    assert tidy_closing("Quer que eu encaminhe para a equipe?\n\nPosso lhe ajudar com algo mais?") == \
        "Quer que eu encaminhe para a equipe?"
    assert tidy_closing("Pronto, está encaminhado. Posso lhe ajudar com algo mais?") == \
        "Pronto, está encaminhado. Posso lhe ajudar com algo mais?"
    # Waiting for the order number: offering more help reads as if the conversation were over.
    assert tidy_closing("Preciso do número do seu pedido e do e-mail. Posso lhe ajudar com algo mais?") == \
        "Preciso do número do seu pedido e do e-mail."


def test_text_in_another_script_is_rewritten_and_trimmed_if_it_stays():
    mixed = "Não posso verificar o clima. Tem algum tipo de violão ou outra乐器相关的问题我可以帮助您解答。"
    assert cut_other_script(mixed) == "Não posso verificar o clima."
    client, events = FakeClient([text_message(mixed), text_message(mixed)]), []
    reply = Agent(load_settings(), client, FakeTools()).respond([], "E a previsão do tempo?", on_event=events.append)
    assert reply.text == "Não posso verificar o clima." and reply.grounding_retries == 1
    assert "outro idioma" in client.requests[1]["messages"][-1]["content"]
    assert ("language", "trimmed") in [(e["check"], e["outcome"]) for e in events if e["type"] == "guard"]

def test_verified_order_becomes_a_session_fact():
    from emporio.agent import session_facts

    trail = [{"name": "consultar_pedido", "arguments": {"numero_pedido": 7, "email_ou_telefone": "a@b.com"},
              "result": {"encontrado": True, "pedido": 7, "cliente": "Letícia", "status": "entregue",
                         "itens": [{"produto": "Shelby SU-21S", "quantidade": 1}],
                         "conclusao_prazos": "TODOS os prazos terminaram."}},
             {"name": "consultar_pedido", "arguments": {"numero_pedido": 9, "email_ou_telefone": "x@y.com"},
              "result": {"encontrado": False}}]
    facts = session_facts(trail)
    assert len(facts) == 1 and "pedido 7 de Letícia" in facts[0] and "a@b.com" in facts[0]
    client = FakeClient([text_message("Certo, Letícia.")])
    Agent(load_settings(), client, FakeTools()).respond([], "E se tiver defeito?", facts=facts)
    assert "Já verificado nesta conversa" in client.requests[0]["messages"][-2]["content"]


def test_every_step_is_emitted_as_a_trace_event():
    from emporio.privacy import PrivacyGuard

    events = []
    client = FakeClient([tool_message("detalhes_produto", {"produto": "GD20"}),
                         text_message("O e-mail da Ana é ana@jmail.com.")])
    ana = __import__("emporio.catalog", fromlist=["Customer"]).Customer(1, "Ana", "(67) 99123-4567", "ana@jmail.com", "CG")
    agent = Agent(load_settings(), client, FakeTools(), guard=PrivacyGuard([ana], "# Regras\n- x"))
    reply = agent.respond([], "Quanto custa o GD20?", on_event=events.append)
    kinds = [e["type"] for e in events]
    assert kinds == ["context", "model_call", "tool", "model_call", "guard", "guard"]
    assert events[2]["name"] == "detalhes_produto" and events[2]["auto"] is False
    assert events[-1]["check"] == "personal_data" and events[-1]["outcome"] == "redacted"
    assert "ana@jmail.com" not in reply.text and reply.trace == events
