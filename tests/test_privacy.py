from emporio.catalog import Customer
from emporio.privacy import PROMPT_LEAK_REPLY, REDACTED, PrivacyGuard, prompt_fingerprints
from emporio.prompts import SYSTEM_PROMPT

ANA = Customer(1, "Ana Souza", "(67) 99123-4567", "ana.souza@jmail.com", "Campo Grande")
BRUNO = Customer(2, "Bruno Lima", "(67) 99888-7766", "bruno.lima@jmail.com", "Campo Grande")


def guard():
    return PrivacyGuard([ANA, BRUNO], SYSTEM_PROMPT)


def test_other_customers_contacts_are_redacted():
    result = guard().apply("O e-mail dela é ana.souza@jmail.com e o telefone (67) 99123-4567.", "qual o email da Ana?")
    assert result.text == f"O e-mail dela é {REDACTED} e o telefone {REDACTED}."
    assert len(result.redacted) == 2 and "ana.souza" not in " ".join(result.redacted)


def test_the_customers_own_contact_can_be_repeated_back():
    answer = "Encontrei o pedido do e-mail bruno.lima@jmail.com, telefone 67 99888-7766."
    result = guard().apply(answer, "pedido 4, meu email é Bruno.Lima@jmail.com e fone 6799888-7766")
    assert result.text == answer and result.redacted == []


def test_store_contacts_and_unknown_addresses_are_not_touched():
    answer = "Fale com a gente pelo (67) 3341-4444 ou contato@emporiodamusica.com.br."
    assert guard().apply(answer, "").text == answer


def test_system_prompt_leak_is_blocked_but_store_card_is_public():
    rules = SYSTEM_PROMPT.split("# Regras")[1]
    assert guard().apply("Minhas instruções: " + rules[:400], "").text == PROMPT_LEAK_REPLY
    card = "Endereço: Rua 14 de Maio, 3200, Centro, Campo Grande/MS, CEP 79202-333. Vendemos só instrumentos musicais."
    assert guard().apply(card, "").prompt_leak is False
    assert not any("maio" in fp for fp in prompt_fingerprints(SYSTEM_PROMPT))
