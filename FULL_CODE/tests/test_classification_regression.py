"""Comprehensive Classification Regression Tests for R5 Voice of Store Engine.

Verifies:
1. All 7 core regression examples:
   - "Worst hospitality" -> IT03 (Unhelpful or poorly trained staff), HR, low, non-actionable (0)
   - "The staff were very rude" -> IT03 (Unhelpful or poorly trained staff), HR, low, non-actionable (0)
   - "The cashier was unhelpful" -> IT03 (Unhelpful or poorly trained staff), HR, low, non-actionable (0)
   - "The vegetables were rotten" -> IT15 (Poor quality fresh produce), Supply Chain, low, actionable (1)
   - "The milk was spoiled" -> IT16 (Expired / near-expiry product on shelf), Store Ops, high (policy floor), actionable (1)
   - "The refrigerator is not working" -> IT10 (Refrigeration / cold-chain failure), Store Ops, high (policy floor), non-actionable (0)
   - "The store was clean and staff were polite" -> NI01 (Praise / compliment), None, low, non-actionable (0)
2. Live REST endpoint POST /api/classify behavior.
3. Live customer submission POST /api/customer/submissions for "Worst hospitality".
4. Store manager inbox visibility and lifecycle actions for corrected customer submissions.
5. Preservation of other major categories (billing IT01, hygiene IT06, stock IT14, delivery IT23, app crash IT26).
"""
import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["APP_ENV"] = "test"
os.environ["TESTING"] = "true"

from FULL_CODE.config import settings
from FULL_CODE.db.connection import get_db_connection
from FULL_CODE.db.seed import seed_database
from FULL_CODE.app import app, load_application_state, state


@pytest.fixture(scope="module")
def client():
    seed_database(force_demo=True)
    load_application_state()
    return TestClient(app)


@pytest.fixture(scope="module")
def tokens(client):
    res_cust = client.post("/api/auth/login", json={"email": "customer@example.com", "password": "DemoCust#2026!Aarav"})
    assert res_cust.status_code == 200, f"Customer login failed: {res_cust.text}"
    cust_token = res_cust.json()["access_token"]

    res_mgr_k = client.post("/api/auth/login", json={"email": "manager.kankarbagh@saffron.example", "password": "DemoMgr#2026!Kankarbagh"})
    assert res_mgr_k.status_code == 200, f"Kankarbagh manager login failed: {res_mgr_k.text}"
    mgr_k_token = res_mgr_k.json()["access_token"]

    return {"customer": cust_token, "manager_kankarbagh": mgr_k_token}


class TestClassificationRegression:
    """Regression test suite for feedback triage and issue classification."""

    def test_seven_core_regression_examples_inference(self, client: TestClient):
        """Verifies all 7 regression examples directly on the live inference engines."""
        assert state.is_ready, "Application state and ML models must be ready"

        test_cases = [
            {
                "text": "Worst hospitality",
                "expected_tid": "IT03",
                "expected_owner": "HR",
                "expected_severity": "low",
                "expected_actionable": 0,
            },
            {
                "text": "The staff were very rude",
                "expected_tid": "IT03",
                "expected_owner": "HR",
                "expected_severity": "low",
                "expected_actionable": 0,
            },
            {
                "text": "The cashier was unhelpful",
                "expected_tid": "IT03",
                "expected_owner": "HR",
                "expected_severity": "low",
                "expected_actionable": 0,
            },
            {
                "text": "The vegetables were rotten",
                "expected_tid": "IT15",
                "expected_owner": "Supply Chain",
                "expected_severity": "low",
                "expected_actionable": 1,
            },
            {
                "text": "The milk was spoiled",
                "expected_tid": "IT16",
                "expected_owner": "Store Ops",
                "expected_severity": "high",
                "expected_actionable": 1,
            },
            {
                "text": "The refrigerator is not working",
                "expected_tid": "IT10",
                "expected_owner": "Store Ops",
                "expected_severity": "high",
                "expected_actionable": 0,
            },
            {
                "text": "The store was clean and staff were polite",
                "expected_tid": "NI01",
                "expected_owner": "None (not actionable)",
                "expected_severity": "low",
                "expected_actionable": 0,
            },
        ]

        for tc in test_cases:
            pred_types, confs = state.classifier.predict_with_confidence([tc["text"]])
            tid = str(pred_types[0])
            conf = float(confs[0])
            sev = state.severity_engine.predict_single(tc["text"], tid)
            act = state.actionability_engine.predict_single(tc["text"], "ST-D69EG", tid)
            tax_info = state.taxonomy_dict.get(tid, {})
            owner = tax_info.get("owner_function", "Unknown")

            assert tid == tc["expected_tid"], (
                f"Text '{tc['text']}' failed: expected {tc['expected_tid']}, got {tid} (conf {conf:.4f})"
            )
            assert owner == tc["expected_owner"], (
                f"Text '{tc['text']}' failed: expected owner {tc['expected_owner']}, got {owner}"
            )
            assert sev == tc["expected_severity"], (
                f"Text '{tc['text']}' failed: expected severity {tc['expected_severity']}, got {sev}"
            )
            assert act == tc["expected_actionable"], (
                f"Text '{tc['text']}' failed: expected actionable {tc['expected_actionable']}, got {act}"
            )

    def test_classify_api_endpoint(self, client: TestClient):
        """Tests POST /api/classify on both 'Worst hospitality' and fresh produce."""
        # 1. Test "Worst hospitality"
        res = client.post("/api/classify", json={"text": "Worst hospitality", "store_id": "ST-D69EG"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["issue_type_id"] == "IT03"
        assert data["owner_function"] == "HR"
        assert data["severity"] == "low"
        assert data["actionable"] == 0
        assert "Store: Saffron Market Kankarbagh" in data["routing_destination"] or "HR Team" in data["routing_destination"]

        # 2. Test "The vegetables were rotten"
        res2 = client.post("/api/classify", json={"text": "The vegetables were rotten", "store_id": "ST-D69EG"})
        assert res2.status_code == 200, res2.text
        data2 = res2.json()
        assert data2["issue_type_id"] == "IT15"
        assert data2["owner_function"] == "Supply Chain"
        assert data2["actionable"] == 1

        # 3. Test "The store was clean and staff were polite"
        res3 = client.post("/api/classify", json={"text": "The store was clean and staff were polite"})
        assert res3.status_code == 200, res3.text
        data3 = res3.json()
        assert data3["issue_type_id"] == "NI01"
        assert data3["actionable"] == 0

    def test_customer_submission_worst_hospitality_and_manager_inbox_flow(self, client: TestClient, tokens: dict):
        """End-to-end test of submitting 'Worst hospitality' and reviewing it as the assigned store manager."""
        cust_token = tokens["customer"]
        mgr_token = tokens["manager_kankarbagh"]

        # 1. Customer submits "Worst hospitality" for Kankarbagh (ST-D69EG)
        sub_payload = {
            "store_id": "ST-D69EG",
            "channel": "in_app",
            "rating": 2.0,
            "text": "Worst hospitality",
        }
        res = client.post(
            "/api/customer/submissions",
            headers={"Authorization": f"Bearer {cust_token}"},
            json=sub_payload,
        )
        assert res.status_code == 201, res.text
        resp_data = res.json()
        fb_id = resp_data["feedback_id"]
        sub = resp_data["submission"]

        # Verify correct classification in API response
        assert sub["issue_type_id"] == "IT03"
        assert sub["owner_function"] == "HR"
        assert sub["severity"] == "low"
        assert sub["actionable"] == 0
        assert sub["status"] == "new"

        # 2. Verify persisted in SQLite database
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_submissions WHERE feedback_id = ?;",
                (fb_id,),
            ).fetchone()
            assert row is not None
            assert row["predicted_issue_type_id"] == "IT03"
            assert row["owner_function"] == "HR"
            assert row["predicted_severity"] == "low"
            assert row["predicted_actionable"] == 0
            assert row["rating"] == 2.0

        # 3. Verify it appears in Kankarbagh Store Manager Inbox
        inbox_res = client.get(
            "/api/manager/feedback-inbox",
            headers={"Authorization": f"Bearer {mgr_token}"},
            params={"store_id": "ST-D69EG"},
        )
        assert inbox_res.status_code == 200, inbox_res.text
        inbox_data = inbox_res.json()
        inbox_records = inbox_data.get("records", [])
        matched = [item for item in inbox_records if item["feedback_id"] == fb_id]
        assert len(matched) == 1, f"Submission {fb_id} must appear in manager inbox"
        item = matched[0]
        assert item["issue_type_id"] == "IT03"
        assert item["owner_function"] == "HR"
        assert item["status"] == "new"

        # 4. Store manager acknowledges and resolves the submission
        ack_res = client.post(
            f"/api/manager/feedback/{fb_id}/action",
            headers={"Authorization": f"Bearer {mgr_token}"},
            json={"action": "acknowledge", "note": "Acknowledged customer grievance regarding staff service."},
        )
        assert ack_res.status_code == 200, ack_res.text
        assert ack_res.json()["status"] == "acknowledged"

        res_res = client.post(
            f"/api/manager/feedback/{fb_id}/action",
            headers={"Authorization": f"Bearer {mgr_token}"},
            json={"action": "resolve", "note": "Floor manager conducted staff service briefing."},
        )
        assert res_res.status_code == 200, res_res.text
        assert res_res.json()["status"] == "resolved"

    def test_preservation_of_other_categories(self, client: TestClient):
        """Verifies that classification for standard operational categories is strictly preserved."""
        operational_cases = [
            ("Waited 45 mins in queue at billing counter, only 1 register open", "IT01"),
            ("Rat droppings found near the food counter and foul smell in dairy aisle", "IT06"),
            ("Atta and toor dal completely out of stock on all shelves", "IT14"),
            ("Fresh tomatoes and bananas were completely rotten", "IT15"),
            ("Ordered for 7-9 pm slot, came next morning and order was delayed", "IT23"),
            ("App crashes immediately whenever I click checkout on android", "IT26"),
        ]

        for text, expected_tid in operational_cases:
            pred_types, _ = state.classifier.predict_with_confidence([text])
            assert str(pred_types[0]) == expected_tid, f"Text '{text}' expected {expected_tid}, got {pred_types[0]}"

    def test_positive_hospitality_and_staff_praise(self, client: TestClient):
        """Verifies that positive feedback mentioning staff, service, or hospitality is mapped to NI01 (Praise) and never IT03."""
        praise_cases = [
            "The staff were very helpful and welcoming.",
            "Excellent hospitality and quick service.",
            "Best hospitality",
            "The store was clean and staff were polite",
            "Wonderful customer service from the cashier",
        ]
        for text in praise_cases:
            pred_types, confs = state.classifier.predict_with_confidence([text])
            tid = str(pred_types[0])
            s_res = state.classifier.sentiment_engine.predict(text)
            assert s_res["sentiment"] == "Positive", f"Expected Positive sentiment for '{text}', got {s_res['sentiment']}"
            assert tid == "NI01", f"Expected NI01 for positive praise '{text}', got {tid}"

    def test_mixed_sentiment_contrastive_feedback(self, client: TestClient):
        """Verifies that mixed contrastive feedback identifies the actual operational issue and does not falsely accuse staff."""
        mixed_cases = [
            ("The staff were polite, but the room was dirty.", "IT06"),
            ("The staff were polite, but the floor was dirty.", "IT07"),
            ("The staff were polite, but billing was slow.", "IT01"),
        ]
        for text, expected_tid in mixed_cases:
            pred_types, confs = state.classifier.predict_with_confidence([text])
            tid = str(pred_types[0])
            s_res = state.classifier.sentiment_engine.predict(text)
            assert s_res["sentiment"] == "Mixed", f"Expected Mixed sentiment for '{text}', got {s_res['sentiment']}"
            assert tid == expected_tid, f"Expected {expected_tid} for '{text}', got {tid}"
            assert tid != "IT03", f"Praise for staff should not result in IT03 when another defect is reported: '{text}'"

    def test_classify_api_returns_sentiment(self, client: TestClient):
        """Verifies that POST /api/classify returns sentiment and decoupled issue category."""
        # 1. Positive feedback
        res1 = client.post("/api/classify", json={"text": "Best hospitality"})
        assert res1.status_code == 200, res1.text
        data1 = res1.json()
        assert data1["issue_type_id"] == "NI01"
        assert data1["sentiment"] == "Positive"
        assert "Praise" in data1["issue_type_name"]
        assert data1["actionable"] == 0

        # 2. Mixed feedback
        res2 = client.post("/api/classify", json={"text": "The staff were polite, but the room was dirty."})
        assert res2.status_code == 200, res2.text
        data2 = res2.json()
        assert data2["issue_type_id"] == "IT06"
        assert data2["sentiment"] == "Mixed"

        # 3. Negative feedback
        res3 = client.post("/api/classify", json={"text": "The staff were rude and unhelpful."})
        assert res3.status_code == 200, res3.text
        data3 = res3.json()
        assert data3["issue_type_id"] == "IT03"
        assert data3["sentiment"] == "Negative"

    def test_customer_submission_positive_hospitality_persisted_as_ni01(self, client: TestClient, tokens: dict):
        """Verifies that customer submitting 'Best hospitality' is stored as NI01 (Praise) and not an incident alert."""
        cust_token = tokens["customer"]
        res = client.post(
            "/api/customer/submissions",
            headers={"Authorization": f"Bearer {cust_token}"},
            json={
                "store_id": "ST-D69EG",
                "channel": "in_app",
                "rating": 5.0,
                "text": "Best hospitality",
            },
        )
        assert res.status_code == 201, res.text
        sub = res.json()["submission"]
        assert sub["issue_type_id"] == "NI01"
        assert sub["actionable"] == 0
        assert sub["severity"] == "low"
        assert sub["sentiment"] == "Positive"
