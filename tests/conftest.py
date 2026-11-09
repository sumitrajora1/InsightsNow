import shutil
import time

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from scripts.seed import seed_sqlite


@pytest.fixture
def settings(tmp_path):
    seed_sqlite(tmp_path / "lending.db")
    shutil.copy(ROOT / "config/policy.json", tmp_path / "policy.json")
    shutil.copy(ROOT / "fixtures/metadata.json", tmp_path / "metadata.json")
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path}/sessions.db",
        demo_data_path=tmp_path / "lending.db",
        policy_path=tmp_path / "policy.json",
        metadata_fixture=tmp_path / "metadata.json",
        query_timeout_seconds=3,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def headers(user="alice"):
    return {"X-Dev-User": user}


def session(client, user="alice"):
    r = client.post("/v1/sessions", headers=headers(user), json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ask(client, sid, question, user="alice"):
    r = client.post(f"/v1/sessions/{sid}/questions", headers=headers(user), json={"question": question})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    return poll(client, rid, user)


def poll(client, rid, user="alice"):
    for _ in range(300):
        response = client.get(f"/v1/requests/{rid}", headers=headers(user))
        assert response.status_code == 200, response.text
        row = response.json()
        if row["status"] not in ("queued", "running"):
            return row
        time.sleep(0.01)
    pytest.fail("Request did not finish")
