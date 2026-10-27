import pytest
from config import settings
from database.seed import seed
from context.context_manager import initialize

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BILLING_DB", tmp_path / "billing.db")
    monkeypatch.setattr(settings, "CONTEXT_DB", tmp_path / "context.db")
    monkeypatch.setattr(settings, "INVOICE_DIR", tmp_path / "invoices")
    seed()
    initialize()
