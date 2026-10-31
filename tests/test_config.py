from datetime import datetime

from emporio.config import load_settings


def test_reference_datetime_freezes_now_in_store_timezone(monkeypatch):
    monkeypatch.setenv("REFERENCE_DATETIME", "2026-11-03T10:30:00")
    settings = load_settings()
    now = settings.now()
    assert now.replace(tzinfo=None) == datetime(2026, 11, 3, 10, 30)
    assert str(now.tzinfo) == "America/Campo_Grande"
