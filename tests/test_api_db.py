import pytest
from fastapi.testclient import TestClient

from groundswell.config import get_settings

pytestmark = pytest.mark.db


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("GS_API_TOKEN", "secret-token")
    get_settings.cache_clear()
    from groundswell.api.app import create_app

    yield TestClient(create_app())
    get_settings.cache_clear()


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_api_requires_token(client):
    assert client.get("/api/meta").status_code == 401
    r = client.get("/api/meta", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    codes = [x["code"] for x in r.json()["regions"]]
    assert {"GC", "SEA", "NA", "LATAM", "EU", "GLOBAL"} <= set(codes)


def test_rankings_fallback_and_reports(client):
    h = {"Authorization": "Bearer secret-token"}
    r = client.get("/api/rankings", params={"region": "GC"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["topics"] == [] and "items" in body and "keywords" in body
    assert client.get("/api/topics/999999", headers=h).status_code == 404
    assert client.get("/api/reports/daily/..%2Fetc", headers=h).status_code in (400, 404)
    assert client.get("/api/health/sources", headers=h).status_code == 200


def test_scheduler_jobs():
    from groundswell.scheduler import JOB_NAMES, build_scheduler

    sched = build_scheduler()
    assert {j.id for j in sched.get_jobs()} == set(JOB_NAMES)
