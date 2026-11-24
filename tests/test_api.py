import hashlib
import hmac
import json
from app.models.schemas import FindingSeverity, ReviewVerdict


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database_connected"] is True
    assert data["llm_provider"] == "mock"


def test_review_diff_clean(client):
    diff = """diff --git a/app/math.py b/app/math.py
index 0000000..1111111 100644
--- /dev/null
+++ b/app/math.py
@@ -0,0 +1,3 @@
+def add(a: int, b: int) -> int:
+    return a + b
+"""
    response = client.post("/review/diff", json={"diff_text": diff, "title": "Clean Math PR"})
    assert response.status_code == 200
    data = response.json()
    assert data["verdict"] == ReviewVerdict.APPROVE.value
    assert data["metrics"]["total_findings"] == 0
    assert data["metrics"]["analyzed_files"] == 1


def test_review_diff_with_security_and_bugs(client):
    diff = """diff --git a/app/auth.py b/app/auth.py
index 0000000..2222222 100644
--- /dev/null
+++ b/app/auth.py
@@ -0,0 +1,8 @@
+import subprocess
+
+API_KEY = "AKIA1111111111111111"
+
+def run_script(cmd):
+    subprocess.run(cmd, shell=True)
+    time.sleep(5)
+"""
    response = client.post("/review/diff", json={"diff_text": diff, "title": "Vulnerable Auth PR"})
    assert response.status_code == 200
    data = response.json()

    assert data["verdict"] == ReviewVerdict.REQUEST_CHANGES.value
    assert len(data["findings"]) >= 2

    # Check for presence of critical/error findings
    severities = [f["severity"] for f in data["findings"]]
    assert FindingSeverity.CRITICAL.value in severities

    # Verify run was saved to SQLite database
    review_id = data["id"]
    get_res = client.get(f"/reviews/{review_id}")
    assert get_res.status_code == 200
    saved_data = get_res.json()
    assert saved_data["review"]["id"] == review_id
    assert len(saved_data["findings"]) >= 2


def test_list_reviews(client):
    response = client.get("/reviews?limit=10")
    assert response.status_code == 200
    reviews = response.json()
    assert isinstance(reviews, list)
    assert len(reviews) >= 1


def test_github_webhook_endpoint(client):
    secret = "test-webhook-secret-12345"
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": 42,
            "head": {"sha": "c0ffee1234"},
        },
        "repository": {
            "full_name": "acme/backend-api",
        },
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    # Note: since review_github_pr fetches from GitHub, we mock or expect HTTP error on invalid repo
    # Or let's test rejection on invalid signature
    bad_sig = "sha256=invalidhashvalue"
    res_rejected = client.post(
        "/webhooks/github",
        content=payload_bytes,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": bad_sig},
    )
    assert res_rejected.status_code == 401

    # Ignored event
    res_ignored = client.post(
        "/webhooks/github",
        content=payload_bytes,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig},
    )
    assert res_ignored.status_code == 200
    assert res_ignored.json()["status"] == "ignored"


def test_review_pr_validation_errors(client):
    # Missing both url and repo
    res1 = client.post("/review/pr", json={})
    assert res1.status_code == 400

    # Malformed PR url
    res2 = client.post("/review/pr", json={"pr_url": "https://invalid-url.com/something"})
    assert res2.status_code == 400
    assert "Invalid GitHub PR URL format" in res2.json()["detail"]

    # Malformed repo without owner
    res3 = client.post("/review/pr", json={"repo": "invalidrepo", "pull_number": 1})
    assert res3.status_code == 422  # Pydantic validation error


def test_get_nonexistent_review(client):
    res = client.get("/reviews/nonexistent-id-0000")
    assert res.status_code == 404
    assert res.json()["detail"] == "Review run not found."

