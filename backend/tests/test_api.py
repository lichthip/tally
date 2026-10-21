"""HTTP API smoke tests (real app, real Postgres, rule-based extractor)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(sf):
    from tally.api import routes
    from tally.api.main import create_app
    routes._submit_limit = None
    with TestClient(create_app()) as c:
        yield c


def test_submit_then_process_then_read(client):
    r = client.post("/api/posts", json={"text": "Long $NVDA into earnings", "author": "u/alice"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and not body["duplicate"]

    from tally.api.deps import get_runtime
    get_runtime().new_worker().run_once(block_ms=500)

    post = client.get(f"/api/posts/{body['post_id']}").json()
    assert post["status"] == "extracted" and post["contributor"] == "alice"
    assert post["ideas"][0]["ticker"] == "NVDA"


def test_idempotency_key_header(client):
    h = {"Idempotency-Key": "abc-123"}
    a = client.post("/api/posts", json={"text": "Long $AAPL", "author": "bob"}, headers=h).json()
    b = client.post("/api/posts", json={"text": "Long $AAPL", "author": "bob"}, headers=h).json()
    assert b["duplicate"] and a["post_id"] == b["post_id"]


def test_validation_rejects_oversized_post(client):
    r = client.post("/api/posts", json={"text": "x" * 6000, "author": "bob"})
    assert r.status_code == 422


def test_admin_endpoints_require_token(client):
    assert client.post("/api/admin/score").status_code == 401
    assert client.post("/api/admin/score", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.post("/api/admin/score", headers={"X-Admin-Token": "test-token"}).status_code == 202


def test_submit_rate_limit(client):
    codes = [client.post("/api/posts", json={"text": f"Long $AAPL {i}", "author": "spammer"}).status_code
             for i in range(6)]
    assert codes[:3] == [202, 202, 202] and 429 in codes


def test_read_endpoints(client):
    for path in ("/api/health", "/api/leaderboard", "/api/ideas", "/api/signals", "/api/posts"):
        assert client.get(path).status_code == 200, path


def test_admin_only_reads(client):
    admin = {"X-Admin-Token": "test-token"}
    for path in ("/api/system", "/api/review", "/api/admin/check"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=admin).status_code == 200, path


def test_retry_failed_requeues_only_failed_posts(client):
    from sqlalchemy import text

    from tally.api.deps import get_runtime
    rt = get_runtime()
    a = client.post("/api/posts", json={"text": "Long $AAPL", "author": "x"}).json()["post_id"]
    b = client.post("/api/posts", json={"text": "Long $MSFT", "author": "y"}).json()["post_id"]
    with rt.sf.begin() as s:
        s.execute(text("UPDATE raw_posts SET status='failed' WHERE id=:a"), {"a": a})
        s.execute(text("UPDATE raw_posts SET status='extracted' WHERE id=:b"), {"b": b})
    r = client.post("/api/admin/retry-failed", headers={"X-Admin-Token": "test-token"})
    assert r.json() == {"requeued": 1}
    assert rt.ingestion.requeue([b])[b].startswith("skipped")
