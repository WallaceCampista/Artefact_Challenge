"""The five tools the agent can call: JSON schemas for the model + deterministic execution.

Design notes
- Few tools with short Portuguese descriptions: a 7B model routes better with less choice.
- Tools are the security boundary, not the prompt: no tool can list customers, and an order
  is only returned when the caller proves to be its owner (order number + e-mail/phone).
- Results are compact JSON with values already formatted in reais, plus an "observacao" field
  when a store rule applies, so the model does not need to do math or remember rules.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from .catalog import AVAILABILITY, ORDER_STATUS, Product, Store, describe_payment_method
from .deadlines import order_deadlines
from .payments import payment_options
from .policies import PolicyIndex
from .text import format_brl

MAX_RESULTS = 10
HANDOFF_REASONS = ["reclamacao", "devolucao_ou_troca", "pedido_atrasado", "atendimento_humano", "outro"]


def tool_schemas(category_names: list[str]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "buscar_produtos",
                "description": (
                    "Busca instrumentos no catálogo: opções por categoria, faixa de preço, marca, "
                    "características ou promoções, e responde se a loja tem um tipo de instrumento. "
                    "Para um modelo específico, prefira detalhes_produto."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "termo": {"type": "string", "description": "Palavras-chave, ex.: 'eletroacústico aço', 'iniciante', 'Yamaha'."},
                        "categoria": {"type": "string", "enum": category_names},
                        "preco_maximo": {"type": "number", "description": "Preço máximo em reais."},
                        "preco_minimo": {"type": "number", "description": "Preço mínimo em reais."},
                        "apenas_promocoes": {"type": "boolean", "description": "Só produtos com promoção ativa."},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "detalhes_produto",
                "description": (
                    "Preço, estoque, especificações, formas de pagamento (PIX, parcelas) e alternativas "
                    "de UM produto específico, pelo nome ou modelo."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"produto": {"type": "string", "description": "Nome ou modelo, ex.: 'Takamine GD20'."}},
                    "required": ["produto"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "consultar_pedido",
                "description": (
                    "Status, itens, rastreio e prazos de troca/devolução de um pedido. Exige o número do "
                    "pedido E o e-mail ou telefone do cadastro; se o cliente não informou os dois, peça antes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "numero_pedido": {"type": "integer"},
                        "email_ou_telefone": {"type": "string"},
                    },
                    "required": ["numero_pedido", "email_ou_telefone"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "consultar_politicas",
                "description": (
                    "Consulta o manual da loja: trocas, devoluções, arrependimento, garantia, frete e prazos de "
                    "entrega, formas de pagamento, parcelamento, regras de promoções, privacidade. O manual NÃO diz "
                    "preços, estoque nem quais promoções estão ativas: para isso use as ferramentas de produto."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"pergunta": {"type": "string", "description": "A dúvida do cliente."}},
                    "required": ["pergunta"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "encaminhar_para_equipe",
                "description": (
                    "Abre um chamado para a equipe humana: reclamação, devolução ou troca que o cliente quer "
                    "iniciar, pedido atrasado ou parado, ou quando o cliente pede para falar com uma pessoa."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "motivo": {"type": "string", "enum": HANDOFF_REASONS},
                        "resumo": {"type": "string", "description": "Resumo do caso em uma ou duas frases."},
                        "contato": {"type": "string", "description": "Nome e telefone ou e-mail do cliente, se informados."},
                    },
                    "required": ["motivo", "resumo"],
                },
            },
        },
    ]


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: dict
    duration_ms: float


@dataclass
class HandoffDesk:
    """Where handoffs go. In-memory by default; the CLI and API persist them in SQLite."""

    tickets: list[dict] = field(default_factory=list)

    def open(self, reason: str, summary: str, contact: str | None, session_id: str | None, today: date) -> str:
        protocol = f"EM-{today:%Y%m%d}-{len(self.tickets) + 1:04d}"
        self.tickets.append({"protocol": protocol, "reason": reason, "summary": summary, "contact": contact})
        return protocol


class ToolBox:
    def __init__(self, store: Store, policies: PolicyIndex, desk: HandoffDesk, clock: Callable[[], datetime]):
        self.store = store
        self.policies = policies
        self.desk = desk
        self.clock = clock  # store-local "now"; a callable so long-running processes stay correct
        self.session_id: str | None = None
        self.schemas = tool_schemas([c.name for c in store.categories.values()])
        self._handlers = {
            "buscar_produtos": self.buscar_produtos,
            "detalhes_produto": self.detalhes_produto,
            "consultar_pedido": self.consultar_pedido,
            "consultar_politicas": self.consultar_politicas,
            "encaminhar_para_equipe": self.encaminhar_para_equipe,
        }

    def run(self, name: str, raw_arguments: str | dict | None) -> ToolCall:
        """Executes a tool call from the model. Errors go back to the model as data, so it can
        fix its own arguments on the next step instead of crashing the conversation."""
        started = time.perf_counter()
        arguments: dict = {}
        try:
            parsed = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("os argumentos devem ser um objeto JSON")
            arguments = parsed
            handler = self._handlers.get(name)
            if handler is None:
                raise ValueError(f"ferramenta desconhecida: {name}")
            result = handler(**arguments)
        except TypeError as exc:  # missing required argument
            result = {"erro": f"argumentos inválidos para {name}: {exc}"}
        except ValueError as exc:  # includes json.JSONDecodeError
            result = {"erro": str(exc)}
        return ToolCall(name, arguments, result, (time.perf_counter() - started) * 1000)

    @property
    def today(self) -> date:
        return self.clock().date()

    # ------------------------------------------------------------------ formatting
    @staticmethod
    def _promotion(product: Product) -> str | None:
        promo = product.active_promotion
        if promo is None:
            return None
        return (f"{promo.discount_percent:g}% de desconto ({promo.description}): de {format_brl(product.price)} "
                f"por {format_brl(product.final_price)}")

    def _summary(self, product: Product) -> dict:
        item = {
            "id": product.id,
            "nome": product.name,
            "preco": format_brl(product.final_price),
            "disponibilidade": AVAILABILITY[product.availability],
        }
        if product.active_promotion:
            item["promocao"] = self._promotion(product)
        if product.sellable and product.stock <= 3:
            item["disponibilidade"] += f" (últimas {product.stock} unidades)"
        return item

    def _named(self, product: Product) -> dict:
        item = self._summary(product)
        expired = [f"{x.description} ({x.discount_percent:g}%)" for x in product.promotions if not x.is_active]
        if expired:
            item["promocoes_encerradas"] = expired
        if not product.sellable:
            item["alternativas"] = [self._summary(x) for x in self.store.similar_products(product)]
        item["observacao"] = ("Este é o modelo que o cliente citou. Informe a disponibilidade dele"
                              + (" e diga que as promoções encerradas não valem mais" if expired else "")
                              + (" e ofereça as alternativas." if not product.sellable else "."))
        return item

    # ------------------------------------------------------------------ tools
    def buscar_produtos(self, termo: str | None = None, categoria: str | None = None,
                        preco_maximo: float | None = None, preco_minimo: float | None = None,
                        apenas_promocoes: bool = False, **_ignored) -> dict:
        category = None
        if categoria:
            category = self.store.resolve_category(categoria)
            if category is None:
                raise ValueError(f"categoria desconhecida: {categoria}")
        elif termo:
            category = self.store.resolve_category(termo)  # "sax", "violino"... typed as a term
            if category is not None:
                termo = None
        if category is not None and not self.store.products_in(category):
            return {
                "total_encontrado": 0,
                "observacao": (f"A loja não tem nenhum produto da categoria {category.name} no catálogo no momento. "
                               "Não invente modelos; ofereça encaminhar para a equipe."),
            }
        filters = dict(category=category, min_price=preco_minimo, max_price=preco_maximo, only_promotions=apenas_promocoes)
        found = self.store.search_products(term=termo, **filters)
        # The customer may name a specific model that the search hides (out of stock, discontinued,
        # outside the price/promotion filter). Small models then answer "not found"; return it explicitly.
        named = self.store.find_product(termo) if termo else []
        named = named[0] if len(named) == 1 and named[0] not in found else None
        note = None
        if not found and termo:
            found = self.store.search_products(**filters)
            if found and not named:
                note = f"Nenhum produto corresponde a '{termo}'; abaixo, os resultados sem esse termo."
        if named:
            result = {"produto_citado": self._named(named)}
            if not found:
                return result
        elif not found:
            return {"total_encontrado": 0, "observacao": "Nenhum produto disponível com esses filtros."}
        else:
            result = {}
        result["total_encontrado"] = len(found)
        result["produtos"] = [self._summary(p) for p in found[:MAX_RESULTS]]
        notes = [note] if note else []
        if len(found) > MAX_RESULTS:
            notes.append(f"Mostrando {MAX_RESULTS} de {len(found)} (os mais relevantes ou mais baratos); "
                         "ofereça refinar a busca.")
        notes.append("Preços já com promoção, quando houver. PIX dá 5% de desconto só em produtos sem promoção.")
        result["observacao"] = " ".join(notes)
        return result

    def detalhes_produto(self, produto: str, **_ignored) -> dict:
        matches = self.store.find_product(str(produto))
        if not matches:
            suggestions = self.store.search_products(term=str(produto))[:3]
            return {
                "encontrado": False,
                "observacao": "Esse produto não está no catálogo da loja. Não invente; diga que não trabalhamos com "
                              "esse modelo e, se houver, ofereça as sugestões.",
                "sugestoes": [self._summary(p) for p in suggestions],
            }
        if len(matches) > 1:
            return {
                "encontrado": False,
                "observacao": "Mais de um produto corresponde. Mostre as opções ou pergunte qual o cliente quer.",
                "candidatos": [self._summary(p) for p in matches],
            }
        p = matches[0]
        details = {
            "id": p.id,
            "nome": p.name,
            "marca": p.brand,
            "categoria": p.category.name,
            "preco_de_tabela": format_brl(p.price),
            "preco_final": format_brl(p.final_price),
            "disponibilidade": AVAILABILITY[p.availability],
            "estoque": p.stock if p.sellable else 0,
            "descricao": p.reliable_description,
            "especificacoes": p.specs_pt(),
        }
        notes = []
        if p.active_promotion:
            details["promocao"] = self._promotion(p)
            notes.append("Informe o preço original, o percentual e o preço final (regra 6.2).")
        expired = [f"{x.description} ({x.discount_percent:g}%)" for x in p.promotions if not x.is_active]
        if expired:
            details["promocoes_encerradas"] = expired
            notes.append("As promoções em promocoes_encerradas NÃO estão valendo; se o cliente perguntar, "
                         "diga que encerraram e informe o preço atual (regra 7.3).")
        if p.description_issue:
            notes.append("A descrição deste produto foi omitida por estar inconsistente; use só nome e especificações.")
        if p.sellable:
            pay = payment_options(p.final_price, on_promotion=p.active_promotion is not None)
            details["pagamento"] = {
                "pix": format_brl(pay["pix"]) + (" (5% de desconto)" if pay["pix_discount_applied"]
                                                 else " (sem desconto extra: produto já em promoção)"),
                "cartao_de_credito": (f"até {pay['credit_max_installments']}x de "
                                      f"{format_brl(pay['credit_installment_value'])} sem juros"),
                "debito_ou_boleto": format_brl(pay["debit"]),
                "pode_combinar_pix_e_cartao": pay["combined_payment_allowed"],
            }
        else:
            details["alternativas"] = [self._summary(x) for x in self.store.similar_products(p)]
            notes.append({
                "out_of_stock": "Sem estoque: diga que está temporariamente indisponível e sugira as alternativas.",
                "discontinued": "Descontinuado: diga que não faz mais parte do catálogo e ofereça as alternativas.",
                "coming_soon": "Ainda não está à venda (em breve): ofereça as alternativas disponíveis.",
            }[p.availability])
        if notes:
            details["observacao"] = " ".join(notes)
        return details

    def consultar_pedido(self, numero_pedido, email_ou_telefone: str = "", **_ignored) -> dict:
        digits = re.sub(r"\D", "", str(numero_pedido))
        if not digits:
            raise ValueError("numero_pedido deve ser um número")
        if not str(email_ou_telefone).strip():
            return {"encontrado": False, "observacao": "Peça ao cliente o e-mail ou telefone do cadastro."}
        order = self.store.find_order(int(digits), str(email_ou_telefone))
        if order is None:
            return {
                "encontrado": False,
                "observacao": "Não há pedido com esse número para esse contato. Peça para o cliente conferir os dados. "
                              "Não diga se o número de pedido existe.",
            }
        result = {
            "encontrado": True,
            "pedido": order.id,
            "cliente": order.customer.first_name,
            "data_da_compra": f"{order.order_date:%d/%m/%Y}",
            "status": ORDER_STATUS.get(order.status, order.status),
            "itens": [{"produto": i.product.name if i.product else f"produto {i.product_id}", "quantidade": i.quantity}
                      for i in order.items],
            "valor_total": format_brl(order.total),
            "pagamento": describe_payment_method(order.payment_method),
        }
        if order.tracking_code:
            result["codigo_de_rastreio"] = order.tracking_code
        if order.estimated_delivery:
            result["previsao_de_entrega"] = f"{order.estimated_delivery:%d/%m/%Y}"
        if order.notes:
            result["observacao_do_pedido"] = order.notes
        result.update(order_deadlines(order, self.today))
        return result

    def consultar_politicas(self, pergunta: str, **_ignored) -> dict:
        sections = self.policies.search(str(pergunta), k=3)
        return {
            "trechos": [{"secao": s.label, "texto": s.body} for s in sections],
            "observacao": "Responda só com base nestes trechos. Se eles não respondem a dúvida, diga que vai "
                          "confirmar com a equipe.",
        }

    def encaminhar_para_equipe(self, motivo: str, resumo: str, contato: str | None = None, **_ignored) -> dict:
        if motivo not in HANDOFF_REASONS:
            motivo = "outro"
        protocol = self.desk.open(motivo, str(resumo), contato, self.session_id, self.today)
        return {
            "protocolo": protocol,
            "prazo_de_retorno": "até 24 horas úteis (regra 7.3)",
            "observacao": "Informe o protocolo e o prazo ao cliente. Se a loja estiver fechada, diga quando reabre.",
        }
