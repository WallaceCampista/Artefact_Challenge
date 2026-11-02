import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from emporio.config import PROJECT_ROOT
from emporio.policies import PolicyIndex
from emporio.tools import HandoffDesk, ToolBox

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=ZoneInfo("America/Campo_Grande"))


@pytest.fixture(scope="module")
def tools(store):
    policies = PolicyIndex.from_pdf(PROJECT_ROOT / "data" / "políticas_da_loja.pdf")
    return ToolBox(store, policies, HandoffDesk(), clock=lambda: NOW)


def call(tools, name, **arguments):
    return tools.run(name, json.dumps(arguments)).result


def test_search_guitars_under_1000(tools):
    result = call(tools, "buscar_produtos", categoria="Violões", preco_maximo=1000)
    assert result["total_encontrado"] == 12
    assert len(result["produtos"]) == 10
    assert result["produtos"][0]["preco"] == "R$ 429,90"
    assert "refinar" in result["observacao"]


def test_category_without_products_is_explicit(tools):
    result = call(tools, "buscar_produtos", termo="saxofone")
    assert result["total_encontrado"] == 0
    assert "Não invente" in result["observacao"]


def test_unmatched_term_falls_back_to_filters(tools):
    result = call(tools, "buscar_produtos", termo="xyzabc", categoria="Ukuleles")
    assert result["total_encontrado"] > 0
    assert "xyzabc" in result["observacao"]


def test_price_and_payment_conditions(tools):
    result = call(tools, "detalhes_produto", produto="Takamine GD20")
    assert result["preco_final"] == "R$ 2.199,00"
    assert result["pagamento"]["pix"].startswith("R$ 2.089,05")
    assert result["pagamento"]["cartao_de_credito"] == "até 12x de R$ 183,25 sem juros"


def test_promotion_shows_original_price_and_no_pix_stacking(tools):
    result = call(tools, "detalhes_produto", produto="Crafter HT-100")
    assert "18%" in result["promocao"] and "R$ 2.399,00" in result["promocao"]
    assert result["preco_final"] == "R$ 1.967,18"
    assert "sem desconto extra" in result["pagamento"]["pix"]


def test_out_of_stock_product_offers_alternatives_and_expired_promotions(tools):
    result = call(tools, "detalhes_produto", produto="Giannini GF-3D")
    assert result["disponibilidade"].startswith("sem estoque")
    assert len(result["alternativas"]) == 3
    assert len(result["promocoes_encerradas"]) == 2
    assert "pagamento" not in result


def test_unknown_product_is_not_invented(tools):
    result = call(tools, "detalhes_produto", produto="Fender Jazz Bass")
    assert result["encontrado"] is False


def test_order_lookup_requires_owner_contact(tools):
    assert call(tools, "consultar_pedido", numero_pedido=7, email_ou_telefone="x@y.com")["encontrado"] is False
    result = call(tools, "consultar_pedido", numero_pedido=7, email_ou_telefone="leticia.rocha@jmail.com")
    assert result["cliente"] == "Letícia"
    assert result["status"] == "entregue"
    regret = result["prazos"][0]
    assert regret["regra"].startswith("4.1") and "encerrado em 24/02/2026" in regret["situacao"]
    assert "@" not in json.dumps(result)  # no e-mail echoed back


def test_order_number_accepts_hash_prefix(tools):
    result = call(tools, "consultar_pedido", numero_pedido="#8", email_ou_telefone="(67) 99654-7821")
    assert result["status"].startswith("enviado")
    assert "alerta" in result  # estimated delivery 28/02/2026 has passed


def test_policies_return_cited_sections(tools):
    result = call(tools, "consultar_politicas", pergunta="Me arrependi da compra, posso devolver?")
    assert any(t["secao"].startswith("4.1") for t in result["trechos"])


def test_handoff_returns_protocol(tools):
    result = call(tools, "encaminhar_para_equipe", motivo="reclamacao", resumo="Produto com risco", contato="Ana")
    assert result["protocolo"].startswith("EM-20261031-")


def test_bad_arguments_become_errors_for_the_model(tools):
    assert "erro" in tools.run("detalhes_produto", "{not json").result
    assert "erro" in tools.run("detalhes_produto", "{}").result
    assert "erro" in tools.run("apagar_banco", "{}").result


def test_wrong_category_points_to_where_results_are(tools):
    result = call(tools, "buscar_produtos", categoria="Guitarras", apenas_promocoes=True)
    assert result["total_encontrado"] == 0
    assert "Violões (2)" in result["observacao"]


def test_hours_tool_resolves_relative_days_and_holidays(store):
    sunday = datetime(2026, 11, 1, 10, 0, tzinfo=ZoneInfo("America/Campo_Grande"))
    tools = ToolBox(store, PolicyIndex([]), HandoffDesk(), clock=lambda: sunday)
    tomorrow = call(tools, "consultar_horario", quando="amanhã")
    assert tomorrow["dia"].startswith("amanhã (segunda-feira, 02/11/2026)")
    assert tomorrow["situacao"] == "FECHADA (feriado: Finados)"
    assert tomorrow["reabre"] == "terça-feira, 03/11 às 09:00"
    assert call(tools, "consultar_horario", quando="sábado")["situacao"] == "aberta das 09:00 às 13:00"
    assert "FECHADA" in call(tools, "consultar_horario")["agora"]
    assert call(tools, "consultar_horario", quando="25/12")["situacao"] == "FECHADA (feriado: Natal)"
    assert call(tools, "consultar_horario", quando="2026-11-15")["situacao"] == "FECHADA (feriado: Proclamação da República)"


def test_handoff_mentions_closed_store_only_when_closed(store):
    sunday = datetime(2026, 11, 1, 10, 0, tzinfo=ZoneInfo("America/Campo_Grande"))
    tuesday = datetime(2026, 11, 3, 10, 30, tzinfo=ZoneInfo("America/Campo_Grande"))
    for now, closed in ((sunday, True), (tuesday, False)):
        tools = ToolBox(store, PolicyIndex([]), HandoffDesk(), clock=lambda now=now: now)
        result = call(tools, "encaminhar_para_equipe", motivo="reclamacao", resumo="teste")
        assert ("loja_fechada_agora" in result) is closed


def test_order_deadlines_come_with_a_computed_conclusion(store):
    from datetime import date

    from emporio.deadlines import order_deadlines

    order = next(o for o in store.orders.values() if o.id == 7)
    late = order_deadlines(order, date(2026, 11, 3))["conclusao_prazos"]
    assert "TODOS os prazos" in late and "fabricante" in late
    early = order_deadlines(order, order.estimated_delivery)["conclusao_prazos"]
    assert "4.1 arrependimento" in early and "TODOS" not in early
