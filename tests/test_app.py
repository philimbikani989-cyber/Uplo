"""Smoke tests for Uploader Ultra Plus. No live network calls or posts."""
import os
os.environ["UUP_ADMIN_KEY"] = "test-key-with-more-than-twenty-four-characters"
os.environ["UUP_ENABLE_LIVE"] = "false"

from fastapi.testclient import TestClient
import app as service

client = TestClient(service.app)
headers = {"Authorization": "Bearer test-key-with-more-than-twenty-four-characters"}

def test_health_safe_default():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["live_publishing_enabled"] is False

def test_dashboard_is_available_on_mobile():
    response = client.get("/")
    assert response.status_code == 200
    assert "viewport" in response.text
    assert "Uploader Ultra Plus" in response.text

def test_private_routes_are_protected():
    assert client.get("/api/posts").status_code == 401
    assert client.get("/api/posts", headers={"Authorization": "Bearer incorrect"}).status_code == 403
    assert client.get("/api/posts", headers=headers).status_code == 200

def test_brand_list():
    response = client.get("/api/brands", headers=headers)
    assert response.status_code == 200
    assert {x["id"] for x in response.json()} == {"bigdawg", "curiosity"}

def test_create_draft_and_simulate_only():
    payload = {
        "brand": "bigdawg",
        "text": "Testing our own publishing engine.",
        "schedule_at": "2020-01-01T12:00:00+02:00"
    }
    response = client.post("/api/posts", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    assert response.json()["status"] == "DRAFT"
    approval = client.post("/api/posts/"+pid+"/approve",headers=headers,json={"narration_verified":False})
    assert approval.status_code == 200
    run = client.post("/api/run",headers=headers)
    assert run.status_code == 200
    assert run.json()["live_mode"] is False
    assert any(x["result"]=="SIMULATED" for x in run.json()["outcomes"])

def test_reject_bad_schedule():
    response=client.post("/api/posts",headers=headers,json={"brand":"bigdawg","text":"Good text","schedule_at":"2026-12-01T10:00:00"})
    assert response.status_code == 422
