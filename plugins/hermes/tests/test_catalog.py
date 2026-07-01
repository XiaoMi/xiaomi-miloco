import pytest

from hermes import catalog


@pytest.fixture(autouse=True)
def _reset():
    catalog._cached["text"] = ""
    catalog._cached["generated_at"] = 0.0
    yield
    catalog._cached["text"] = ""
    catalog._cached["generated_at"] = 0.0


def test_get_catalog_failure_returns_empty_string(monkeypatch):
    monkeypatch.setattr(catalog, "_run_cli_catalog", lambda: None)
    assert catalog.get_catalog() == ""


def test_get_catalog_success_returns_catalog_text(monkeypatch):
    monkeypatch.setattr(catalog, "_run_cli_catalog", lambda: "DEVICE CATALOG TEXT")
    assert catalog.get_catalog() == "DEVICE CATALOG TEXT"


def test_get_catalog_cache_hit_within_throttle(monkeypatch):
    calls = iter(["FIRST", "SECOND"])
    monkeypatch.setattr(catalog, "_run_cli_catalog", lambda: next(calls))
    first = catalog.get_catalog()
    second = catalog.get_catalog()
    assert first == "FIRST"
    assert second == "FIRST"
