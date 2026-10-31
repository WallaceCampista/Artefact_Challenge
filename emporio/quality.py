"""Data quality checks behind `python -m emporio data-report`.

Every finding is computed from the data (nothing is hard-coded), together with how the
agent handles it, so the report stays true if the CSVs change.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .catalog import Store
from .text import format_brl

TRACKING_CODE = re.compile(r"^BR[A-Z0-9]{9}BR$")  # policy 5.3
STALE_AFTER_DAYS = 30


@dataclass(frozen=True)
class Issue:
    area: str
    finding: str
    handling: str


def data_quality_issues(store: Store, today: date) -> list[Issue]:
    issues: list[Issue] = []
    products = sorted(store.products.values(), key=lambda p: p.id)

    for p in products:
        if p.description_issue:
            issues.append(Issue(
                "products", f"#{p.id} {p.name}: {p.description_issue}",
                "description hidden from the agent; name, price, stock and specs are trusted",
            ))
    for p in products:
        if p.status != "active" and p.stock > 0:
            issues.append(Issue(
                "products", f"#{p.id} {p.name}: status '{p.status}' but {p.stock} units in stock",
                "not sold; the agent says it is discontinued / coming soon and offers alternatives",
            ))
        elif p.status == "active" and p.stock == 0:
            issues.append(Issue(
                "products", f"#{p.id} {p.name}: active but out of stock",
                "reported as temporarily unavailable with similar in-stock alternatives (policy 7.3)",
            ))
    for category in store.categories.values():
        if not store.products_in(category):
            issues.append(Issue(
                "categories", f"'{category.name}' has no products, although the manual says the store sells them",
                "the agent says there are no models in the catalog and offers a human contact; it never invents products",
            ))

    promotions = [promo for p in products for promo in p.promotions]
    inactive = [promo for promo in promotions if not promo.is_active]
    issues.append(Issue(
        "promotions", f"{len(inactive)} of {len(promotions)} promotions are inactive",
        "only active ones change prices; expired ones are disclosed as expired (policy 7.3)",
    ))
    for p in products:
        if len(p.promotions) > 1:
            active = sum(1 for promo in p.promotions if promo.is_active)
            issues.append(Issue(
                "promotions", f"#{p.id} {p.name} has {len(p.promotions)} promotions ({active} active)",
                "the active one with the highest discount applies (none stack, policy 6.2)" if active
                else "all expired: the list price applies",
            ))

    for order in sorted(store.orders.values(), key=lambda o: o.id):
        if order.items and abs(order.items_total - order.total) > 0.01:
            issues.append(Issue(
                "orders", f"order {order.id}: total {format_brl(order.total)} != items at list price {format_brl(order.items_total)}",
                "total_brl is what was charged and is the value reported",
            ))
        not_sellable = [i.product for i in order.items if i.product and i.product.status != "active"]
        for product in not_sellable:
            issues.append(Issue(
                "orders", f"order {order.id} contains #{product.id} {product.name} ({product.status})",
                "historical order, reported as is",
            ))
        age = (today - order.order_date).days
        if order.status in ("pending", "confirmed") and age > STALE_AFTER_DAYS:
            issues.append(Issue(
                "orders", f"order {order.id} is '{order.status}' for {age} days",
                "status reported as is, and the agent offers to escalate to the team",
            ))
        if order.tracking_code and not TRACKING_CODE.match(order.tracking_code):
            issues.append(Issue(
                "orders", f"order {order.id}: tracking code {order.tracking_code} does not follow BR + 9 + BR",
                "reported as is",
            ))
        if order.status in ("shipped", "delivered") and not order.tracking_code:
            issues.append(Issue("orders", f"order {order.id} is {order.status} without a tracking code", "reported as is"))

    delivered = [o for o in store.orders.values() if o.status == "delivered"]
    issues.append(Issue(
        "orders", f"{len(delivered)} delivered orders have only an estimated delivery date",
        "the estimated date is used as the receipt date for return and warranty deadlines (assumption)",
    ))

    by_email = defaultdict(list)
    for customer in store.customers.values():
        by_email[customer.email.lower()].append(customer.id)
    for email, ids in by_email.items():
        if len(ids) > 1:
            issues.append(Issue(
                "customers", f"e-mail {email} is shared by customers {ids}",
                "order lookups need order number + contact, so a shared e-mail never exposes another order",
            ))
    return issues


def print_report(store: Store, today: date) -> None:
    issues = data_quality_issues(store, today)
    print(f"Data quality report — {len(store.products)} products, {len(store.orders)} orders, "
          f"{len(store.customers)} customers (reference date {today:%Y-%m-%d})\n")
    current = None
    for issue in issues:
        if issue.area != current:
            current = issue.area
            print(f"[{current}]")
        print(f"  - {issue.finding}\n      -> {issue.handling}")
    print(f"\n{len(issues)} findings.")
