from __future__ import annotations

import importlib
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client_with_data_root(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("INVENTORY_DATA_ROOT", str(tmp_path))

    api_root = Path(__file__).resolve().parents[2] / "api"
    sys.path.insert(0, str(api_root))

    import inventory_hub.settings as settings_module
    import inventory_hub.config_io as config_io_module
    import inventory_hub.routers.shops as shops_module
    importlib.reload(settings_module)
    importlib.reload(config_io_module)
    importlib.reload(shops_module)

    app = FastAPI()
    app.include_router(shops_module.router)
    return TestClient(app)


def test_shop_config_reads_from_data_root(tmp_path, monkeypatch) -> None:
    client = _client_with_data_root(tmp_path, monkeypatch)
    config_path = tmp_path / "shops" / "biketrek" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"upgates_login": "demo"}', encoding="utf-8")

    response = client.get("/shops/biketrek/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["upgates_login"] == "demo"


def test_shop_config_missing_returns_404(tmp_path, monkeypatch) -> None:
    client = _client_with_data_root(tmp_path, monkeypatch)

    response = client.get("/shops/missing/config")

    assert response.status_code == 404
    detail = response.json()["detail"]
    expected_path = (tmp_path / "shops" / "missing" / "config.json").resolve()
    assert str(expected_path) in detail
