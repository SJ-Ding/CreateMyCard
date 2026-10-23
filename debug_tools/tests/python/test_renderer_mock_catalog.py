"""浏览器模拟目录不调用真实能力，且版本选择不会静默兜底。"""

from types import SimpleNamespace

import pytest
from debug_tools.end_to_end_debug.backend.debug_agent.config import DebugSettings
from debug_tools.end_to_end_debug.backend.server import create_app
from debug_tools.static_site import create_frontend_app
from fastapi.testclient import TestClient


@pytest.fixture(params=["frontend", "full"])
def client(request):
    if request.param == "frontend":
        app = create_frontend_app()
    else:
        app = create_app(DebugSettings(), production_settings=SimpleNamespace())
    return TestClient(app)


def test_catalog_defaults_are_read_only_registry_definitions(client):
    response = client.get("/debug/renderer/mock-catalog")
    assert response.status_code == 200
    body = response.json()
    assert body.get("source") == "default"
    assert body.get("registryVersion") in body.get("availableVersions", [])
    definitions = body.get("capabilities")
    assert isinstance(definitions, list)
    weather = next(item for item in definitions if item.get("id") == "ViewWeather")
    assert weather.get("defaultWriteResultTo") == "/data/weather"
    assert isinstance(weather.get("outputSchema"), dict)
    assert "dependencies" not in weather
    assert "inputSchema" not in weather


@pytest.mark.parametrize("version", ["", "../", "missing", "/etc/passwd"])
def test_catalog_rejects_unknown_versions_without_fallback(client, version):
    response = client.get("/debug/renderer/mock-catalog", params={"registryVersion": version})
    assert response.status_code == 422
    detail = response.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("availableVersions")


def test_catalog_uses_request_version_interval(client):
    response = client.get(
        "/debug/renderer/mock-catalog", params={"appVersion": "11.7.7.330", "romVersion": "7.0"}
    )
    assert response.status_code == 200
    assert response.json().get("source") == "request"


def test_catalog_explicit_version_precedes_incompatible_request(client):
    original = client.get("/debug/renderer/mock-catalog").json()
    response = client.get(
        "/debug/renderer/mock-catalog",
        params={
            "registryVersion": original.get("registryVersion"),
            "appVersion": "0.1",
            "romVersion": "0.1",
        },
    )
    assert response.status_code == 200
    assert response.json().get("source") == "artifact_or_manual"


def test_catalog_unmatched_request_returns_choices(client):
    response = client.get(
        "/debug/renderer/mock-catalog", params={"appVersion": "0.1", "romVersion": "0.1"}
    )
    assert response.status_code == 422
    detail = response.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("availableVersions")
