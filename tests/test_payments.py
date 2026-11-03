import pytest

from emporio.payments import installment_values, max_installments, payment_options, pix_price


@pytest.mark.parametrize(
    "price, expected",
    [
        (49.0, 1),      # below the R$ 50 minimum even in 2x
        (159.9, 3),     # 3x of 53,30
        (349.9, 4),     # 4x of 87,48 (5x would be 69,98 < 80)
        (599.9, 6),     # 6x of 99,98 (7x would be 85,70 < 100)
        (1200.0, 12),   # 12x of 100
        (2199.0, 12),
    ],
)
def test_max_installments_follow_section_3_1(price, expected):
    assert max_installments(price) == expected


def test_pix_discount_does_not_stack_with_promotions():
    assert pix_price(2199.0, on_promotion=False) == 2089.05
    assert pix_price(1967.18, on_promotion=True) == 1967.18


def test_combined_payment_only_above_2000():
    assert payment_options(2000.0, False)["combined_payment_allowed"] is False
    assert payment_options(2000.01, False)["combined_payment_allowed"] is True


def test_every_allowed_installment_count_is_listed():
    assert installment_values(4299.0)[10] == 429.9 and installment_values(4299.0)[12] == 358.25
    assert list(installment_values(349.9)) == [2, 3, 4]  # 5x would be below the R$ 80 minimum
    assert installment_values(49.0) == {}  # below R$ 50 even in 2x: single payment only
