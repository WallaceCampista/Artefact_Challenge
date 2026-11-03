"""Loads the challenge CSVs into typed objects and answers catalog and order queries.

Data treatment decisions (see the README for the full list):
- Only products with status "active" and stock > 0 are sellable.
- Only promotions with is_active = 1 change the price; inactive ones are kept so the agent can
  say a promotion has ended (policy 7.3) instead of pretending it never existed.
- A product description that names another brand, or contradicts the specs, is not shown:
  name, price, stock and specs are treated as the source of truth.
- Order lookups require the order number plus the customer's e-mail or phone (LGPD).
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from .text import BM25, normalize, stems, tokens

KNOWN_BRANDS = (
    "Music Man", "Yamaha", "Tagima", "Giannini", "Rozini", "Shelby", "Crafter", "Martin",
    "Taylor", "Takamine", "Kala", "Ohana", "Kalani", "Fender", "Gibson", "Ibanez", "PRS",
    "Pearl", "Tama", "Korg", "Nord", "Roland",
)

# Words customers use for each category (category_id -> aliases). Used to resolve the
# category argument and to make "violão" or "sax" match products in a free-text search.
CATEGORY_ALIASES = {
    1: ["guitarra", "guitarras"],
    2: ["baixo", "baixos", "contrabaixo", "bass"],
    3: ["bateria", "baterias", "percussao"],
    4: ["teclado", "teclados", "piano", "pianos", "sintetizador", "synth"],
    5: ["violao", "violoes"],
    6: ["sopro", "madeiras", "flauta", "clarinete", "saxofone", "sax", "oboe"],
    7: ["metais", "trompete", "trombone", "tuba", "trompa"],
    8: ["cordas orquestrais", "violino", "viola", "violoncelo", "cello", "contrabaixo acustico"],
    9: ["ukulele", "ukuleles"],
}

SPEC_LABELS = {
    "top": "tampo", "back_sides": "fundo e laterais", "neck": "braço", "strings": "cordas",
    "scale": "escala", "electronics": "eletrônica (captação)", "color": "cor", "body": "corpo",
    "shells": "cascos", "pieces": "peças", "hardware": "ferragens", "keys": "teclas",
    "type": "tipo", "polyphony": "polifonia",
}
SPEC_VALUES = {"steel": "aço", "nylon": "nylon", "yes": "sim", "no": "não", "included": "inclusas"}

AVAILABILITY = {
    "available": "disponível",
    "out_of_stock": "sem estoque no momento",
    "discontinued": "descontinuado (fora de linha)",
    "coming_soon": "em breve (ainda não está à venda)",
}

ORDER_STATUS = {
    "pending": "pendente (aguardando confirmação)",
    "confirmed": "confirmado (em preparação para envio)",
    "shipped": "enviado (em transporte)",
    "delivered": "entregue",
    "cancelled": "cancelado",
}

PAYMENT_METHODS = {"pix": "PIX", "debit": "cartão de débito", "boleto": "boleto bancário"}


def describe_payment_method(code: str) -> str:
    match = re.fullmatch(r"credit_(\d+)x", code)
    if match:
        return f"cartão de crédito em {match.group(1)}x"
    return PAYMENT_METHODS.get(code, code)


def detect_brand(text: str) -> str | None:
    norm = f" {normalize(text)} "
    found = [(norm.find(f" {normalize(b)} "), b) for b in KNOWN_BRANDS if f" {normalize(b)} " in norm]
    return min(found)[1] if found else None


def mentioned_brands(text: str) -> set[str]:
    norm = f" {normalize(text)} "
    return {b for b in KNOWN_BRANDS if f" {normalize(b)} " in norm}


def _mention_keys(product: Product) -> set[str]:
    """How an answer names a product: the start of its name up to the model number
    ("yamaha c40", "music man bass 1x"), also from the brand on ("korg synth 1" for
    "Teclado Sintetizador Korg Synth 1"). Names without a number keep two words ("tagima dallas")."""
    words = normalize(product.name).split()
    starts = {0}
    if product.brand and normalize(product.brand).split()[0] in words:
        starts.add(words.index(normalize(product.brand).split()[0]))
    keys = set()
    for start in starts:
        rest = words[start:]
        end = next((i + 1 for i, word in enumerate(rest) if any(ch.isdigit() for ch in word)), min(2, len(rest)))
        keys.add(" ".join(rest[:end]))
    return keys


@dataclass
class Category:
    id: int
    name: str
    description: str


@dataclass
class Promotion:
    id: int
    product_id: int
    discount_percent: float
    description: str
    is_active: bool


@dataclass
class Product:
    id: int
    name: str
    price: float
    category: Category
    description: str
    stock: int
    status: str
    specs: dict
    created_at: date
    brand: str | None = None
    description_issue: str | None = None  # why the description is not trusted, if it isn't
    promotions: list[Promotion] = field(default_factory=list)
    search_stems: list[str] = field(default_factory=list)

    @property
    def active_promotion(self) -> Promotion | None:
        active = [p for p in self.promotions if p.is_active]
        return max(active, key=lambda p: p.discount_percent) if active else None

    @property
    def final_price(self) -> float:
        promo = self.active_promotion
        if promo is None:
            return self.price
        return round(self.price * (100 - promo.discount_percent) / 100, 2)

    @property
    def availability(self) -> str:
        if self.status == "discontinued":
            return "discontinued"
        if self.status == "coming_soon":
            return "coming_soon"
        return "available" if self.stock > 0 else "out_of_stock"

    @property
    def sellable(self) -> bool:
        return self.availability == "available"

    @property
    def reliable_description(self) -> str | None:
        return None if self.description_issue else self.description

    def specs_pt(self) -> dict:
        return {
            SPEC_LABELS.get(key, key): SPEC_VALUES.get(str(value).lower(), value)
            for key, value in self.specs.items()
        }


@dataclass
class Customer:
    id: int
    name: str
    phone: str
    email: str
    city: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    def matches_contact(self, contact: str) -> bool:
        contact = contact.strip().lower()
        if "@" in contact:
            return contact == self.email.lower()
        given = re.sub(r"\D", "", contact)
        mine = re.sub(r"\D", "", self.phone)
        if len(given) < 8:
            return False
        return mine.endswith(given) or given.endswith(mine)


@dataclass
class OrderItem:
    product_id: int
    quantity: int
    product: Product | None


@dataclass
class Order:
    id: int
    customer: Customer
    order_date: date
    status: str
    total: float
    payment_method: str
    tracking_code: str | None
    estimated_delivery: date | None
    notes: str | None
    items: list[OrderItem] = field(default_factory=list)

    @property
    def items_total(self) -> float:
        return round(sum(i.product.price * i.quantity for i in self.items if i.product), 2)


def _read_csv(data_dir: Path, table: str) -> list[dict]:
    """Finds the file by table name, so the original file names can stay untouched."""
    matches = sorted(data_dir.glob(f"*{table}.csv"))
    if not matches:
        raise FileNotFoundError(f"no CSV for table '{table}' in {data_dir}")
    with matches[0].open(encoding="utf-8", newline="") as handle:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def _description_issue(product: Product) -> str | None:
    others = mentioned_brands(product.description) - ({product.brand} if product.brand else set())
    if others:
        return f"description names another brand ({', '.join(sorted(others))})"
    norm = normalize(product.description)
    for unit, spec_key in (("cordas", "strings"), ("teclas", "keys")):
        spec = str(product.specs.get(spec_key, ""))
        match = re.search(rf"\b(\d+) {unit}\b", norm)
        if match and spec.isdigit() and match.group(1) != spec:
            return f"description says {match.group(1)} {unit}, specs say {spec}"
    return None


class Store:
    """In-memory view of the store's operational data."""

    def __init__(self, categories, products, customers, orders):
        self.categories: dict[int, Category] = categories
        self.products: dict[int, Product] = products
        self.customers: dict[int, Customer] = customers
        self.orders: dict[int, Order] = orders

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, data_dir: Path) -> "Store":
        categories = {
            int(r["category_id"]): Category(int(r["category_id"]), r["name"], r["description"])
            for r in _read_csv(data_dir, "categories")
        }
        products: dict[int, Product] = {}
        for r in _read_csv(data_dir, "products"):
            product = Product(
                id=int(r["product_id"]),
                name=r["name"],
                price=float(r["price_brl"]),
                category=categories[int(r["category_id"])],
                description=r["description"],
                stock=int(r["stock_quantity"]),
                status=r["status"],
                specs=json.loads(r["specs"]) if r["specs"] else {},
                created_at=date.fromisoformat(r["created_at"]),
            )
            product.brand = detect_brand(product.name)
            product.description_issue = _description_issue(product)
            products[product.id] = product

        for r in _read_csv(data_dir, "promotions"):
            promo = Promotion(
                id=int(r["promotion_id"]),
                product_id=int(r["product_id"]),
                discount_percent=float(r["discount_percent"]),
                description=r["description"],
                is_active=r["is_active"] == "1",
            )
            if promo.product_id in products:
                products[promo.product_id].promotions.append(promo)

        for product in products.values():
            # One occurrence per stem: in short product texts, a word repeated in the
            # description ("7 cordas com cordas de aço") must not outweigh a missing one.
            product.search_stems = list(dict.fromkeys(stems(cls._search_text(product))))

        customers = {
            int(r["customer_id"]): Customer(int(r["customer_id"]), r["name"], r["phone"], r["email"], r["city"])
            for r in _read_csv(data_dir, "customers")
        }
        orders: dict[int, Order] = {}
        for r in _read_csv(data_dir, "orders"):
            order = Order(
                id=int(r["order_id"]),
                customer=customers[int(r["customer_id"])],
                order_date=date.fromisoformat(r["order_date"]),
                status=r["status"],
                total=float(r["total_brl"]),
                payment_method=r["payment_method"],
                tracking_code=r["tracking_code"] or None,
                estimated_delivery=_optional_date(r["estimated_delivery"]),
                notes=r["notes"] or None,
            )
            orders[order.id] = order
        for r in _read_csv(data_dir, "order_items"):
            product_id = int(r["product_id"])
            orders[int(r["order_id"])].items.append(
                OrderItem(product_id, int(r["quantity"]), products.get(product_id))
            )
        return cls(categories, products, customers, orders)

    @staticmethod
    def _search_text(product: Product) -> str:
        parts = [product.name, product.category.name, *CATEGORY_ALIASES.get(product.category.id, [])]
        if product.reliable_description:
            parts.append(product.reliable_description)
        # Specs become phrases customers actually type ("cordas de aço", "eletroacústico").
        for key, value in product.specs.items():
            value = str(value).lower()
            if key == "strings" and value in ("steel", "nylon"):
                parts.append(f"cordas de {SPEC_VALUES[value]}")
            elif key == "strings" and value.isdigit():
                parts.append(f"{value} cordas")
            elif key == "keys":
                parts.append(f"{value} teclas")
            elif key == "electronics":
                parts.append("eletroacustico eletrico captacao" if value == "yes" else "acustico")
            else:
                parts.append(value)
        if product.active_promotion:
            parts.append("promocao oferta desconto")
        return " ".join(parts)

    # ---------------------------------------------------------------- catalog
    def resolve_category(self, text: str) -> Category | None:
        wanted = normalize(text)
        for category in self.categories.values():
            if normalize(category.name) == wanted:
                return category
        for category_id, aliases in CATEGORY_ALIASES.items():
            if wanted in aliases or any(alias in wanted.split() for alias in aliases if " " not in alias):
                return self.categories[category_id]
        return None

    def products_in(self, category: Category) -> list[Product]:
        return [p for p in self.products.values() if p.category.id == category.id]

    def search_products(
        self,
        term: str | None = None,
        category: Category | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        only_promotions: bool = False,
        include_unavailable: bool = False,
    ) -> list[Product]:
        """All matches, most relevant first (or cheapest first when there is no term)."""
        pool = list(self.products.values())
        if category is not None:
            pool = [p for p in pool if p.category.id == category.id]
        if not include_unavailable:
            pool = [p for p in pool if p.sellable]
        if only_promotions:
            pool = [p for p in pool if p.active_promotion]
        if min_price is not None:
            pool = [p for p in pool if p.final_price >= min_price]
        if max_price is not None:
            pool = [p for p in pool if p.final_price <= max_price]
        if not term or not stems(term):
            return sorted(pool, key=lambda p: p.final_price)

        query = list(dict.fromkeys(stems(term)))
        scores = BM25([p.search_stems for p in pool]).scores(query)
        # Coordination level first: a product that matches every word of the search beats one
        # that matches a single word many times. Only the best-covered products are returned.
        matched = [sum(1 for q in query if q in p.search_stems) for p in pool]
        best = max(matched, default=0)
        if best == 0:
            return []
        ranked = [(s, p) for s, m, p in zip(scores, matched, pool) if m == best]
        return [p for _, p in sorted(ranked, key=lambda sp: (-sp[0], sp[1].final_price))]

    def find_product(self, query: str) -> list[Product]:
        """[product] for a confident match, several candidates if ambiguous, [] if none."""
        query = query.strip()
        if query.isdigit():
            product = self.products.get(int(query))
            return [product] if product else []
        compact = re.sub(r"(?<=[a-z]) (?=\d)", "", normalize(query))  # "gd 20" -> "gd20"
        wanted = tokens(compact)
        if not wanted:
            return []
        scored = []
        for product in self.products.values():
            name_tokens = set(tokens(product.name))
            for alias in CATEGORY_ALIASES.get(product.category.id, []):
                name_tokens.update(alias.split())
            exact = sum(1 for t in wanted if t in name_tokens)
            partial = sum(
                1 for t in wanted
                if t not in name_tokens and any(n.startswith(t) or (len(n) >= 3 and t.startswith(n)) for n in name_tokens)
            )
            coverage = (exact + 0.5 * partial) / len(wanted)
            similarity = SequenceMatcher(None, compact, normalize(product.name)).ratio()
            scored.append((coverage, exact, similarity, product))
        scored.sort(key=lambda s: (-s[0], -s[1], -s[2]))
        best_coverage = scored[0][0]
        if best_coverage < 0.5:
            return []
        tied = [s[3] for s in scored if s[0] == best_coverage]
        return tied[:6]

    def products_mentioned(self, text: str) -> list[Product]:
        """Catalog products named in free text, such as an answer or a message, by brand and model:
        "o Yamaha C40 custa…" -> [Yamaha C40 Nylon Natural]. A model name that is not in the
        catalog ("Cort S-250HF") matches nothing."""
        padded = f" {normalize(text)} "
        return [p for p in self.products.values() if any(f" {key} " in padded for key in _mention_keys(p))]

    def similar_products(self, product: Product, limit: int = 3) -> list[Product]:
        """Available alternatives in the same category, closest in price (policy 7.3)."""
        pool = [p for p in self.products_in(product.category) if p.sellable and p.id != product.id]
        return sorted(pool, key=lambda p: abs(p.final_price - product.final_price))[:limit]

    # ---------------------------------------------------------------- orders
    def find_order(self, order_id: int, contact: str) -> Order | None:
        """The order only if the contact belongs to its customer. A wrong contact and an
        unknown order look the same to the caller, so order numbers cannot be probed."""
        order = self.orders.get(order_id)
        if order is None or not order.customer.matches_contact(contact):
            return None
        return order
