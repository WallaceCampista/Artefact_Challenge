"""After-sales deadlines from the policy manual, computed for a given order.

LLMs are unreliable at date arithmetic, so the agent receives the dates already computed.
The rules themselves are also retrieved from the manual (consultar_politicas), so the
answer can quote them; this module only does the calendar math.

Assumptions (README): orders in the system are online orders, and since the data has no
actual delivery date, the estimated delivery date is taken as the receipt date.
"""
from __future__ import annotations

from datetime import date, timedelta

from .catalog import Order

REGRET_DAYS = 7  # 4.1 — from receipt
PREFERENCE_EXCHANGE_DAYS = 7  # 4.3 — read as from receipt
DEFECT_EXCHANGE_DAYS = 30  # 4.2 — from purchase
LEGAL_WARRANTY_DAYS = 90  # 8.1 — from receipt
STALE_ORDER_DAYS = 30


def _window(label: str, start: date, days: int, today: date) -> dict:
    last_day = start + timedelta(days=days)
    if today <= last_day:
        status = f"dentro do prazo (até {last_day:%d/%m/%Y})"
    else:
        status = f"prazo encerrado em {last_day:%d/%m/%Y}"
    return {"regra": label, "situacao": status}


def _conclusion(windows: list[dict], today: date) -> str:
    """The verdict in one sentence, so the model does not have to compare dates."""
    open_rules = [w["regra"].split(":")[0] for w in windows if w["situacao"].startswith("dentro")]
    if not open_rules:
        return (f"Hoje ({today:%d/%m/%Y}) TODOS os prazos da loja já terminaram: não cabe arrependimento, troca "
                "nem garantia legal. Resta só a garantia do fabricante (8.2), que a loja pode intermediar.")
    return f"Hoje ({today:%d/%m/%Y}) ainda estão abertos: {', '.join(open_rules)}. Os demais já terminaram."


def order_deadlines(order: Order, today: date) -> dict:
    if order.status == "delivered":
        received = order.estimated_delivery or order.order_date
        windows = [
            _window("4.1 arrependimento: 7 dias após o recebimento", received, REGRET_DAYS, today),
            _window("4.3 troca por preferência: 7 dias", received, PREFERENCE_EXCHANGE_DAYS, today),
            _window("4.2 troca por defeito: 30 dias após a compra", order.order_date, DEFECT_EXCHANGE_DAYS, today),
            _window("8.1 garantia legal: 90 dias após o recebimento", received, LEGAL_WARRANTY_DAYS, today),
        ]
        return {
            "recebimento_considerado": f"{received:%d/%m/%Y} (data prevista de entrega; a data real não consta no sistema)",
            "conclusao_prazos": _conclusion(windows, today),
            "prazos": windows + [
                {"regra": "8.2 garantia do fabricante",
                 "situacao": "6 meses a 2 anos conforme o certificado do produto; a loja pode intermediar (4.2)"},
            ],
        }
    if order.status == "shipped":
        info = {"prazos": ["O prazo de 7 dias para arrependimento (4.1) começa a contar quando o cliente receber o pedido."]}
        if order.estimated_delivery and order.estimated_delivery < today:
            late = (today - order.estimated_delivery).days
            info["alerta"] = (f"A previsão de entrega ({order.estimated_delivery:%d/%m/%Y}) passou há {late} dias. "
                              "Ofereça encaminhar para a equipe verificar com a transportadora.")
        return info
    if order.status in ("pending", "confirmed"):
        info = {"prazos": ["Pedido ainda não enviado. Cancelamento ou alteração: encaminhar para a equipe."]}
        age = (today - order.order_date).days
        if age > STALE_ORDER_DAYS:
            info["alerta"] = f"Pedido parado neste status há {age} dias. Ofereça encaminhar para a equipe."
        return info
    return {}
