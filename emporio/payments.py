"""Payment rules from the policy manual (section 3), applied deterministically.

The manual is ambiguous in two places; the interpretation chosen is documented in the README:
- The payment table says "minimum installment R$ 100,00" for credit cards, while section 3.1
  defines tiers. The more specific rule (3.1) wins.
- "Up to 3x: no minimum installment (except below R$ 50,00)" is read as a R$ 50,00 minimum.
"""
from __future__ import annotations

PIX_DISCOUNT_PERCENT = 5  # section 3: PIX gets 5% off the list price
MAX_INSTALLMENTS = 12
# (largest number of installments in the tier, minimum installment value) — section 3.1
INSTALLMENT_TIERS = ((3, 50.0), (6, 80.0), (12, 100.0))
COMBINED_PAYMENT_ABOVE = 2000.0  # section 3.1: PIX + card allowed above R$ 2.000,00


def minimum_installment(installments: int) -> float:
    for tier_max, minimum in INSTALLMENT_TIERS:
        if installments <= tier_max:
            return minimum
    raise ValueError(f"at most {MAX_INSTALLMENTS} installments")


def max_installments(price: float) -> int:
    """Largest interest-free installment count allowed for this price (1 = single payment)."""
    for n in range(MAX_INSTALLMENTS, 1, -1):
        if price / n >= minimum_installment(n):
            return n
    return 1


def pix_price(price: float, on_promotion: bool) -> float:
    """Section 6.2: the PIX discount does not stack on top of a promotional price."""
    if on_promotion:
        return round(price, 2)
    return round(price * (100 - PIX_DISCOUNT_PERCENT) / 100, 2)


def installment_values(price: float) -> dict[int, float]:
    """Every interest-free option the customer may choose, 2x up to the maximum: {2: 1099.5, ...}.
    Asked "e em 10x?", the model can quote a value a tool computed instead of dividing by itself."""
    return {n: round(price / n, 2) for n in range(2, max_installments(price) + 1)}


def payment_options(price: float, on_promotion: bool) -> dict:
    installments = max_installments(price)
    return {
        "pix": pix_price(price, on_promotion),
        "pix_discount_applied": not on_promotion,
        "credit_max_installments": installments,
        "credit_installment_value": round(price / installments, 2),
        "credit_installments": installment_values(price),
        "debit": round(price, 2),
        "boleto": round(price, 2),
        "combined_payment_allowed": price > COMBINED_PAYMENT_ABOVE,
    }
