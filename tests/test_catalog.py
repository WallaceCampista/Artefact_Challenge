from datetime import date

from emporio.quality import data_quality_issues


def test_guitars_up_to_1000_are_the_twelve_sellable_ones(store):
    violoes = store.resolve_category("violões")
    found = store.search_products(category=violoes, max_price=1000)
    assert len(found) == 12
    assert [p.final_price for p in found] == sorted(p.final_price for p in found)
    assert all(p.sellable for p in found)
    ids = {p.id for p in found}
    assert 96 not in ids  # out of stock
    assert 113 not in ids  # discontinued


def test_price_filter_uses_promotional_price(store):
    # Crafter HT-100 (#90) lists at R$ 2.399 with an active 18% promotion -> R$ 1.967,18
    product = store.products[90]
    assert product.active_promotion.discount_percent == 18
    assert product.final_price == 1967.18
    assert product in store.search_products(max_price=2000)


def test_find_product_by_name_variants(store):
    for query in ("Takamine GD20", "takamine gd-20", "Takamine GD 20", "GD20"):
        assert [p.id for p in store.find_product(query)] == [95], query
    assert store.products[95].final_price == 2199


def test_find_product_prefers_exact_model(store):
    assert [p.id for p in store.find_product("Martin D-28")] == [93]
    assert [p.id for p in store.find_product("Martin D-28E")] == [108]


def test_find_product_ambiguous_and_missing(store):
    assert len(store.find_product("ukulele concert kala")) == 2
    assert store.find_product("Fender Jazz Bass") == []


def test_term_search_ranks_full_matches_first(store):
    found = store.search_products(term="violão eletroacústico cordas de aço")
    assert found
    assert all(p.specs.get("strings") == "steel" and p.specs.get("electronics") == "yes" for p in found)


def test_availability_states(store):
    assert store.products[96].availability == "out_of_stock"
    assert store.products[113].availability == "discontinued"
    assert store.products[136].availability == "discontinued"  # 13 units, still not sold
    assert store.products[130].availability == "coming_soon"


def test_similar_products_are_available_and_same_category(store):
    out_of_stock = store.products[96]
    alternatives = store.similar_products(out_of_stock)
    assert len(alternatives) == 3
    assert all(p.sellable and p.category.id == out_of_stock.category.id for p in alternatives)


def test_unreliable_descriptions_are_flagged(store):
    flagged = {p.id for p in store.products.values() if p.description_issue}
    assert {135, 137, 139, 140, 142, 144} <= flagged
    assert 145 not in flagged and 95 not in flagged


def test_empty_categories_resolve_but_have_no_products(store):
    sopro = store.resolve_category("sax")
    assert sopro is not None and store.products_in(sopro) == []


def test_order_requires_matching_contact(store):
    assert store.find_order(8, "anacarol.ferreira@coldmail.com").status == "shipped"
    assert store.find_order(8, "(67) 99654-7821").id == 8
    assert store.find_order(8, "+55 67 99654-7821").id == 8
    assert store.find_order(8, "someone.else@coldmail.com") is None
    assert store.find_order(8, "7821") is None  # too short to identify anyone
    assert store.find_order(999, "anacarol.ferreira@coldmail.com") is None


def test_shared_email_cannot_open_other_customers_orders(store):
    # bruno.martins@jmail.com belongs to customers 11 and 43; order 10 is customer 11's.
    assert store.find_order(10, "bruno.martins@jmail.com").customer.id == 11


def test_quality_report_finds_known_issues(store):
    findings = " ".join(i.finding for i in data_quality_issues(store, date(2026, 10, 31)))
    assert "order 3: total" in findings and "order 20: total" in findings
    assert "bruno.martins@jmail.com" in findings
    assert "Cordas Orquestrais" in findings


def test_products_are_recognized_by_brand_and_model_inside_a_sentence(store):
    def names(text):
        return [p.name for p in store.products_mentioned(text)]
    assert names("Em 12x, o Yamaha Bass 3X fica R$ 358,25.") == ["Yamaha Bass 3X Electric Bass"]
    assert names("o teclado Korg Synth 1 sai por") == ["Teclado Sintetizador Korg Synth 1 Pro"]
    assert names("gostei do martin d28") == ["Martin D-28 Dreadnought Natural"]  # not the D-28E
    assert names("Temos o Cort S-250HF por R$ 4.299,00.") == []  # not in the catalog
    assert all(store.products_mentioned(p.name) for p in store.products.values())
