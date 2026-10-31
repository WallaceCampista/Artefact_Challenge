"""Opening hours (policy manual, section 2) and holidays.

The manual says the store is closed on holidays but does not list them. Assumption: national
fixed-date holidays, Good Friday, the Mato Grosso do Sul state holiday and Campo Grande's
anniversary. Carnival and Corpus Christi (optional days off) are not modelled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# weekday() -> (opens, closes); Sunday closed
OPENING_HOURS = {
    0: (time(9), time(18)),
    1: (time(9), time(18)),
    2: (time(9), time(18)),
    3: (time(9), time(18)),
    4: (time(9), time(18)),
    5: (time(9), time(13)),
}

FIXED_HOLIDAYS = {
    (1, 1): "Confraternização Universal",
    (4, 21): "Tiradentes",
    (5, 1): "Dia do Trabalho",
    (8, 26): "Aniversário de Campo Grande",
    (9, 7): "Independência do Brasil",
    (10, 11): "Criação do Estado de Mato Grosso do Sul",
    (10, 12): "Nossa Senhora Aparecida",
    (11, 2): "Finados",
    (11, 15): "Proclamação da República",
    (11, 20): "Dia da Consciência Negra",
    (12, 25): "Natal",
}

WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def holiday_name(day: date) -> str | None:
    if (day.month, day.day) in FIXED_HOLIDAYS:
        return FIXED_HOLIDAYS[(day.month, day.day)]
    if day == easter(day.year) - timedelta(days=2):
        return "Sexta-feira Santa"
    return None


def hours_on(day: date) -> tuple[time, time] | None:
    if holiday_name(day):
        return None
    return OPENING_HOURS.get(day.weekday())


@dataclass(frozen=True)
class StoreStatus:
    is_open: bool
    closes_at: datetime | None  # when open
    next_opening: datetime | None  # when closed
    holiday: str | None  # holiday name if today is one

    def describe(self, now: datetime) -> str:
        """One sentence in Portuguese for the system prompt."""
        if self.is_open:
            return f"A loja está ABERTA agora e fecha hoje às {self.closes_at:%H:%M}."
        reason = f" (hoje é feriado: {self.holiday})" if self.holiday else ""
        when = self.next_opening
        if when.date() == now.date():
            day_label = "hoje"
        elif when.date() == now.date() + timedelta(days=1):
            day_label = "amanhã"
        else:
            day_label = f"{WEEKDAYS_PT[when.weekday()]}, {when:%d/%m}"
        return f"A loja está FECHADA agora{reason}. Reabre {day_label} às {when:%H:%M}."


def store_status(now: datetime) -> StoreStatus:
    today = now.date()
    hours = hours_on(today)
    if hours and hours[0] <= now.time() < hours[1]:
        return StoreStatus(True, datetime.combine(today, hours[1], now.tzinfo), None, None)
    # Closed: find the next opening, today (before opening time) or on a following day.
    for offset in range(0, 15):
        day = today + timedelta(days=offset)
        day_hours = hours_on(day)
        if not day_hours:
            continue
        opening = datetime.combine(day, day_hours[0], now.tzinfo)
        if opening > now:
            return StoreStatus(False, None, opening, holiday_name(today))
    raise RuntimeError("no opening found in the next two weeks")
