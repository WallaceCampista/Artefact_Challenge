"""Output guards for personal data and the system prompt (defense in depth).

Access control already lives in the tools: none of them lists customers, and an order lookup
needs the order number plus the registered contact. These guards check the *final text* anyway,
so a future tool, a prompt injection or a model slip still cannot put another customer's e-mail
or phone, or the agent's own instructions, in front of the user.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .catalog import Customer
from .text import normalize

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}(?!\d)")
REDACTED = "[dado protegido]"
PROMPT_LEAK_REPLY = (
    "Desculpe, não posso compartilhar minhas instruções internas. Posso te ajudar com instrumentos, "
    "preços, pedidos ou com as políticas da loja?"
)
# Sections of the system prompt the customer may legitimately hear back (store card, tone).
PUBLIC_SECTIONS = ("# Dados fixos da loja", "# Jeito de falar")
FINGERPRINT_CHARS = 45


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def prompt_fingerprints(system_prompt: str) -> list[str]:
    """Normalized prefixes of the private lines of the prompt (routing rules, flows, policies)."""
    prints, public = [], False
    for line in system_prompt.splitlines():
        if line.startswith("#"):
            public = line.strip().startswith(PUBLIC_SECTIONS)
            heading = normalize(line)
            if not public and len(heading) >= 15:
                prints.append(heading)
            continue
        text = normalize(line)
        if not public and len(text) >= FINGERPRINT_CHARS:
            prints.append(text[:FINGERPRINT_CHARS])
    return prints


@dataclass
class GuardResult:
    text: str
    redacted: list[str] = field(default_factory=list)  # what was removed, masked, for the audit trail
    prompt_leak: bool = False


class PrivacyGuard:
    def __init__(self, customers: Iterable[Customer], system_prompt: str):
        customers = list(customers)
        self.emails = {c.email.strip().lower() for c in customers if c.email}
        self.phones = {_digits(c.phone)[-8:] for c in customers if len(_digits(c.phone)) >= 8}
        self.fingerprints = prompt_fingerprints(system_prompt)

    def apply(self, answer: str, user_text: str) -> GuardResult:
        """`user_text` is everything the customer typed in this conversation: data they gave us
        themselves (their own e-mail or phone) may be repeated back to them."""
        if any(fp in normalize(answer) for fp in self.fingerprints):
            return GuardResult(PROMPT_LEAK_REPLY, prompt_leak=True)

        given = user_text.lower()
        given_digits = _digits(user_text)
        redacted: list[str] = []

        def mask_email(match: re.Match) -> str:
            email = match.group(0).lower().rstrip(".")
            if email in self.emails and email not in given:
                redacted.append(f"e-mail {email[:2]}***")
                return REDACTED
            return match.group(0)

        def mask_phone(match: re.Match) -> str:
            last8 = _digits(match.group(0))[-8:]
            if last8 in self.phones and last8 not in given_digits:
                redacted.append(f"telefone ***{last8[-2:]}")
                return REDACTED
            return match.group(0)

        text = PHONE.sub(mask_phone, EMAIL.sub(mask_email, answer))
        return GuardResult(text, redacted)
