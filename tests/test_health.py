"""Tests for SafeRoute root and health endpoints."""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_root_endpoint():
    """Verify GET / returns a successful welcome response."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "SafeRoute" in data.get("message", "")
    assert data.get("status") == "active"


def test_health_endpoint():
    """Verify GET /health returns the exact expected payload."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "application": "SafeRoute"
    }
