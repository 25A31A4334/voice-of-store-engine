"""Regression and Integration Tests for Google Gemini Hybrid AI Integration.

Verifies:
1. Operational status endpoint (/api/gemini/status) never leaks API keys.
2. PII sanitization redacts emails, phone numbers, and invoice numbers before transmission.
3. Fallback resilience: structured responses succeed even when GEMINI_API_KEY is unconfigured.
4. Schema conformance: GeminiAnalysisResult matches all required fields.
5. Live triage integration: /api/classify supports optional gemini_insights.
6. RBAC & Store Isolation: Store Managers cannot analyze feedback from other stores (HTTP 403).
7. HQ Admin fleet briefing: /api/admin/gemini/network-summary restricted to administrators.
8. Store Manager alert briefing: /api/manager/gemini/alert-brief provides contextual hypotheses.
"""

import pytest
from fastapi.testclient import TestClient
from FULL_CODE.app import app, load_application_state, state
from FULL_CODE.services.gemini_service import sanitize_text_for_ai, gemini_service
from FULL_CODE.services.auth_service import AuthService
from FULL_CODE.db.seed import seed_database

# Ensure database and ML state are initialized for tests
seed_database(force_demo=True)
load_application_state()

client = TestClient(app)


def get_token_for_user(email: str) -> str:
    """Helper to generate signed test JWT tokens for seeded users."""
    claims = {
        "sub": email,
        "email": email,
    }
    return AuthService.create_access_token(claims)


def test_gemini_status_endpoint_security():
    """Verify Gemini status endpoint returns operational metadata and NEVER leaks API key."""
    response = client.get("/api/gemini/status")
    assert response.status_code == 200
    data = response.json()

    assert "configured" in data
    assert "provider" in data
    assert data["provider"] == "Google Gemini"
    assert "model" in data
    assert "status" in data
    assert "timeout_seconds" in data
    assert data["timeout_seconds"] == 8

    # CRITICAL SECURITY CHECK: API key must never be leaked
    assert "api_key" not in data
    assert "key" not in data
    assert "GEMINI_API_KEY" not in str(data)


def test_pii_sanitization():
    """Verify PII (emails, phone numbers, invoice/bill codes) is sanitized before external AI processing."""
    raw = (
        "Customer Aarav Sharma (aarav.sharma@example.com, phone +91-9876543210) "
        "reported that order bill#INV-982341 at the checkout counter was double charged."
    )
    clean = sanitize_text_for_ai(raw)

    assert "aarav.sharma@example.com" not in clean
    assert "[EMAIL_REDACTED]" in clean
    assert "9876543210" not in clean
    assert "[PHONE_REDACTED]" in clean
    assert "INV-982341" not in clean
    assert "[REF_REDACTED]" in clean


def test_gemini_analyze_unconfigured_fallback():
    """Verify POST /api/gemini/analyze returns valid structured schema with deterministic fallback when unconfigured."""
    payload = {
        "text": "Long lines at billing counter and milk bottles were warm and leaking.",
        "store_id": "ST-D69EG",
        "store_name": "Patna Boring Road",
    }
    response = client.post("/api/gemini/analyze", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Schema validation
    assert "summary" in data and len(data["summary"]) > 0
    assert "detected_issues" in data and isinstance(data["detected_issues"], list)
    assert len(data["detected_issues"]) >= 1
    assert "contextual_explanation" in data
    assert "possible_root_causes" in data and isinstance(data["possible_root_causes"], list)
    assert len(data["possible_root_causes"]) >= 1
    assert "recommended_actions" in data and isinstance(data["recommended_actions"], list)
    assert len(data["recommended_actions"]) >= 1
    assert "urgency_explanation" in data
    assert "language_detected" in data
    assert "confidence" in data
    assert "disclaimer" in data
    assert "human" in data["disclaimer"].lower() or "review" in data["disclaimer"].lower()

    # Model status must be valid
    assert data["model_status"] in ("active", "unconfigured", "fallback")


def test_classify_live_feedback_with_gemini_insights():
    """Verify /api/classify attaches gemini_insights when requested."""
    payload = {
        "text": "The fresh produce section had rotten tomatoes and the cashier was extremely rude.",
        "store_id": "ST-D69EG",
        "include_gemini": True,
    }
    response = client.post("/api/classify", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["issue_type_id"] is not None
    assert "gemini_insights" in data
    assert data["gemini_insights"] is not None

    insights = data["gemini_insights"]
    assert "summary" in insights
    assert "possible_root_causes" in insights
    assert "recommended_actions" in insights


def test_manager_gemini_analyze_store_boundary_enforcement():
    """Verify Store Managers cannot use Gemini to inspect feedback belonging to another store (403 Forbidden)."""
    # Manager assigned strictly to ST-D69EG
    manager_token = get_token_for_user("manager.kankarbagh@saffron.example")

    from FULL_CODE.app import state
    other_store_feedback_id = None
    for f_id, f_data in state.feedback_by_id.items():
        sid = f_data.get("store_id")
        if sid and sid.upper() != "ST-D69EG":
            other_store_feedback_id = f_id
            break

    if other_store_feedback_id:
        response = client.post(
            f"/api/manager/feedback/{other_store_feedback_id}/gemini-analyze",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert response.status_code == 403
        assert "Forbidden" in response.json()["detail"]


def test_manager_gemini_alert_brief():
    """Verify /api/manager/gemini/alert-brief delivers structured hypotheses and actions for store manager."""
    manager_token = get_token_for_user("manager.kankarbagh@saffron.example")
    response = client.get(
        "/api/manager/gemini/alert-brief",
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert response.status_code == 200
    data = response.json()

    assert "alert_id" in data
    assert "contextual_summary" in data
    assert "hypotheses" in data and isinstance(data["hypotheses"], list)
    assert "recommended_store_actions" in data and isinstance(data["recommended_store_actions"], list)
    assert "disclaimer" in data


def test_admin_gemini_network_summary_rbac():
    """Verify /api/admin/gemini/network-summary requires admin role."""
    # Unauthenticated -> 401
    unauth_resp = client.get("/api/admin/gemini/network-summary")
    assert unauth_resp.status_code in (401, 403)

    # Customer token -> 403
    cust_token = get_token_for_user("customer@example.com")
    cust_resp = client.get(
        "/api/admin/gemini/network-summary",
        headers={"Authorization": f"Bearer {cust_token}"},
    )
    assert cust_resp.status_code == 403

    # Admin token -> 200
    admin_token = get_token_for_user("admin@saffron.example")
    admin_resp = client.get(
        "/api/admin/gemini/network-summary",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert admin_resp.status_code == 200
    data = admin_resp.json()

    assert "executive_summary" in data
    assert "key_patterns" in data and len(data["key_patterns"]) >= 1
    assert "recommended_hq_interventions" in data and len(data["recommended_hq_interventions"]) >= 1
    assert "disclaimer" in data

    assert "executive_summary" in data
    assert "key_patterns" in data and len(data["key_patterns"]) >= 1
    assert "recommended_hq_interventions" in data and len(data["recommended_hq_interventions"]) >= 1
    assert "disclaimer" in data
