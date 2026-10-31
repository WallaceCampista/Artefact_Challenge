from datetime import date, datetime
from zoneinfo import ZoneInfo

from emporio.hours import easter, holiday_name, store_status

TZ = ZoneInfo("America/Campo_Grande")


def at(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


def test_weekday_open_and_closing_time():
    status = store_status(at(2026, 11, 3, 10, 30))  # Tuesday
    assert status.is_open and status.closes_at.hour == 18


def test_saturday_closes_at_13():
    assert store_status(at(2026, 10, 31, 12, 59)).is_open
    status = store_status(at(2026, 10, 31, 13, 0))
    assert not status.is_open
    # Sunday closed, Monday 02/11 is Finados -> reopens Tuesday 03/11 at 09:00
    assert status.next_opening == at(2026, 11, 3, 9, 0)


def test_holidays():
    assert holiday_name(date(2026, 11, 2)) == "Finados"
    assert easter(2026) == date(2026, 4, 5)
    assert holiday_name(date(2026, 4, 3)) == "Sexta-feira Santa"
    status = store_status(at(2026, 11, 2, 10, 0))
    assert not status.is_open and status.holiday == "Finados"
    assert "feriado" in status.describe(at(2026, 11, 2, 10, 0))


def test_before_opening_reopens_same_day():
    status = store_status(at(2026, 11, 4, 7, 0))
    assert not status.is_open and status.next_opening == at(2026, 11, 4, 9, 0)
    assert "hoje às 09:00" in status.describe(at(2026, 11, 4, 7, 0))
