import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from trino.auth import BasicAuthentication
from trino.exceptions import HttpError, TrinoUserError

from app.auth import Authenticator, Policy
from app.config import Settings
from app.contracts import Identity
from app.enterprise import LoginGuard, RangerPolicy, authenticate_password
from app.errors import AppError
from app.execution import ExecutionControl, SQLiteDemoExecutor, TrinoExecutor
from app.main import create_app


class FakeCursor:
    query_id = "test-trino-query"
    description = [("exposure", "decimal")]

    def __init__(self, responder):
        self.responder = responder
        self.rows = []
        self.queries = []
        self.cancelled = False

    def execute(self, sql):
        self.queries.append(sql)
        self.rows = self.responder(sql)

    def fetchmany(self, size):
        return self.rows[:size]

    def cancel(self):
        self.cancelled = True


class FakeDB:
    def __init__(self, responder):
        self.cur = FakeCursor(responder)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def directory_settings(settings):
    settings.auth_mode = "trino_ldaps"
    settings.authorization_provider = "trino_ranger"
    settings.trino_auth = "ldap_password"
    settings.trino_scheme = "https"
    settings.executor = "trino"
    settings.ldaps_identity_namespace = "bank-directory-test"
    data = json.loads(settings.policy_path.read_text())
    data["ranger_policy_revision"] = "test-r1"
    data["users"] = {"alice": ["lending_analyst"], "bob": ["uae_analyst"]}
    settings.policy_path.write_text(json.dumps(data))
    return settings


def denied():
    return TrinoUserError({"errorName": "PERMISSION_DENIED", "errorType": "USER_ERROR", "message": "Denied"})


def test_ldaps_login_uses_verified_tls_identity_and_excludes_password(settings, monkeypatch):
    directory_settings(settings)
    seen = []
    db = FakeDB(lambda sql: [["alice"]])

    def connect(**kwargs):
        seen.append(kwargs)
        return db

    monkeypatch.setattr("app.enterprise.connect", connect)
    auth = Authenticator(settings)
    header = "Basic " + base64.b64encode(b"alice:test-only-password").decode()
    identity = auth.authenticate(SimpleNamespace(headers={"Authorization": header}))
    assert identity.owner == "ldaps:bank-directory-test:alice"
    assert identity.roles == ["lending_analyst"]
    assert identity.password.get_secret_value() == "test-only-password"
    assert "test-only-password" not in identity.model_dump_json() + repr(identity)
    assert isinstance(seen[0]["auth"], BasicAuthentication)
    assert seen[0]["user"] == "alice" and seen[0]["http_scheme"] == "https"
    assert seen[0]["verify"] is True and seen[0]["max_attempts"] == 1
    assert db.closed and db.cur.cancelled
    assert db.cur.queries == ["SELECT current_user"]


@pytest.mark.parametrize(
    "header", ["", "Bearer fake", "Basic !!!", "Basic " + base64.b64encode(b"alice:").decode()]
)
def test_ldaps_rejects_missing_or_empty_credentials(settings, header):
    directory_settings(settings)
    with pytest.raises(AppError) as exc:
        Authenticator(settings).authenticate(SimpleNamespace(headers={"Authorization": header}))
    assert exc.value.code == "unauthenticated"


def test_alias_mapping_and_transport_fail_closed(settings, monkeypatch):
    directory_settings(settings)
    monkeypatch.setattr("app.enterprise.connect", lambda **kwargs: FakeDB(lambda sql: [["another_user"]]))
    with pytest.raises(AppError, match="identity mapping"):
        authenticate_password(settings, "alice", "test", LoginGuard(settings))
    monkeypatch.setattr(
        "app.enterprise.connect", lambda **kwargs: (_ for _ in ()).throw(OSError("TLS failure"))
    )
    with pytest.raises(AppError) as exc:
        authenticate_password(settings, "alice", "test", LoginGuard(settings))
    assert exc.value.code == "identity_unavailable"


def test_failed_credentials_are_throttled(settings, monkeypatch):
    directory_settings(settings)
    settings.login_failure_limit = 2
    monkeypatch.setattr(
        "app.enterprise.connect", lambda **kwargs: (_ for _ in ()).throw(HttpError("error 401: denied"))
    )
    guard = LoginGuard(settings)
    for _ in range(2):
        with pytest.raises(AppError) as exc:
            authenticate_password(settings, "alice", "wrong", guard)
        assert exc.value.status == 401
    with pytest.raises(AppError) as exc:
        authenticate_password(settings, "alice", "wrong", guard)
    assert exc.value.code == "login_throttled"


def test_ranger_intersection_removes_denied_assets_and_rechecks(settings, monkeypatch):
    directory_settings(settings)
    blocked = False
    seen = []

    def responder(sql):
        seen.append(sql)
        if blocked or "facilities" in sql:
            raise denied()
        return []

    monkeypatch.setattr("app.enterprise.connect", lambda **kwargs: FakeDB(responder))
    identity = Identity(owner="a", username="alice", roles=["lending_analyst"], password=SecretStr("test"))
    policy = RangerPolicy(settings, Policy(settings.policy_path))
    grant = policy.grant(identity)
    assert list(grant.columns) == ["demo_hive.lending.exposure_snapshots"]
    assert all(s.endswith("WHERE FALSE LIMIT 0") for s in seen)
    assert all("*" not in s for s in seen)
    blocked = True
    with pytest.raises(AppError) as exc:
        policy.grant(identity)
    assert exc.value.code == "access_denied"


def test_ranger_outage_never_falls_back_to_registry(settings, monkeypatch):
    directory_settings(settings)
    monkeypatch.setattr("app.enterprise.connect", lambda **kwargs: (_ for _ in ()).throw(OSError("down")))
    identity = Identity(owner="a", username="alice", roles=["lending_analyst"], password=SecretStr("test"))
    with pytest.raises(AppError) as exc:
        RangerPolicy(settings, Policy(settings.policy_path)).grant(identity)
    assert exc.value.code == "authorization_unavailable"


def test_password_execution_uses_same_user_and_never_a_service_account(settings, monkeypatch):
    directory_settings(settings)
    seen = []
    db = FakeDB(lambda sql: [[123]])

    def connect(**kwargs):
        seen.append(kwargs)
        return db

    monkeypatch.setattr("app.execution.connect", connect)
    identity = Identity(owner="a", username="alice", roles=[], password=SecretStr("test"))
    result = TrinoExecutor(settings).execute("SELECT 123", identity, ExecutionControl(2), "correlation")
    assert result.query_id == "test-trino-query"
    assert seen[0]["user"] == "alice" and isinstance(seen[0]["auth"], BasicAuthentication)
    assert seen[0]["verify"] is True and seen[0]["max_attempts"] == 1
    identity.password = SecretStr("")
    with pytest.raises(AppError) as exc:
        TrinoExecutor(settings).execute("SELECT 123", identity, ExecutionControl(2), "c")
    assert exc.value.code == "identity_unavailable" and len(seen) == 1


def test_api_directory_flow_does_not_persist_password(settings, monkeypatch):
    directory_settings(settings)
    monkeypatch.setattr(
        "app.enterprise.connect",
        lambda **kwargs: FakeDB(lambda sql: [[kwargs["user"]]] if sql == "SELECT current_user" else []),
    )
    app = create_app(settings, executor=SQLiteDemoExecutor(settings))
    with TestClient(app) as c:
        alice = ("alice", "synthetic-password-never-store")
        sid = c.post("/v1/sessions", json={}, auth=alice).json()["id"]
        row = c.post(
            f"/v1/sessions/{sid}/questions", json={"question": "exposure last six months"}, auth=alice
        )
        rid = row.json()["id"]
        import time

        for _ in range(200):
            response = c.get(f"/v1/requests/{rid}", auth=alice)
            if response.json()["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert response.json()["status"] == "completed", response.text
        audit = c.get(f"/v1/requests/{rid}/audit", auth=alice)
        assert alice[1] not in response.text + audit.text
        assert c.get(f"/v1/requests/{rid}", auth=("bob", "test")).status_code == 404
        registry = json.loads(settings.policy_path.read_text())
        registry["ranger_policy_revision"] = "test-r2"
        settings.policy_path.write_text(json.dumps(registry))
        assert c.get(f"/v1/requests/{rid}", auth=alice).status_code == 403
        assert "synthetic-password-never-store" not in settings.database_url
        from sqlalchemy import text

        with app.state.store.engine.connect() as db:
            for table in ("conversations", "query_requests", "audit_events"):
                assert alice[1] not in str(db.execute(text(f"SELECT * FROM {table}")).fetchall())


@pytest.mark.parametrize(
    "patch",
    [
        {"trino_scheme": "http"},
        {"trino_auth": "demo"},
        {"authorization_provider": "local"},
        {"ldaps_identity_namespace": ""},
    ],
)
def test_ldaps_configuration_refuses_insecure_or_unverified_modes(patch):
    kwargs = dict(
        auth_mode="trino_ldaps",
        executor="trino",
        trino_auth="ldap_password",
        trino_scheme="https",
        authorization_provider="trino_ranger",
        ldaps_identity_namespace="test",
    )
    Settings(**kwargs)
    with pytest.raises(ValueError):
        Settings(**(kwargs | patch))


def test_ldaps_ui_signin_logout_and_expiry(monkeypatch):
    import httpx
    from streamlit.testing.v1 import AppTest

    from app.config import ROOT

    monkeypatch.setenv("EDQ_UI_AUTH_MODE", "ldaps")
    monkeypatch.setenv("EDQ_API_URL", "https://api.test")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(kwargs)
        assert isinstance(kwargs["auth"], httpx.BasicAuth)
        if url.endswith("/me"):
            data = {"username": "alice", "synthetic": False, "executor": "trino"}
        elif url.endswith("/sessions"):
            data = [{"id": "s1", "title": "Test"}]
        else:
            data = {"id": "s1", "title": "Test", "history": []}
        return httpx.Response(200, json=data)

    monkeypatch.setattr("httpx.get", lambda url, **kwargs: fake_request("GET", url, **kwargs))
    monkeypatch.setattr("httpx.request", fake_request)
    at = AppTest.from_file(ROOT / "ui/app.py", default_timeout=30).run()
    assert not at.exception
    at.text_input[0].set_value("alice")
    at.text_input[1].set_value("test-only-password")
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception
    assert at.session_state["directory_password"].get_secret_value() == "test-only-password"
    assert not at.text_input
    assert calls
    next(b for b in at.button if b.label == "Sign out").click().run()
    assert not at.exception and len(at.text_input) == 2
    assert "directory_password" not in at.session_state
    at.text_input[0].set_value("alice")
    at.text_input[1].set_value("test-only-password")
    next(b for b in at.button if b.label == "Sign in").click().run()
    at.session_state["directory_expiry"] = 0
    at.run()
    assert "directory_password" not in at.session_state and len(at.text_input) == 2


def test_production_ldaps_settings_and_plaintext_rejection():
    settings = Settings(
        environment="production",
        auth_mode="trino_ldaps",
        executor="trino",
        trino_auth="ldap_password",
        trino_scheme="https",
        authorization_provider="trino_ranger",
        ldaps_identity_namespace="bank",
        metadata_provider="openmetadata",
        om_url="https://metadata.test",
        om_expected_version="tested-version",
        llm_provider="bedrock",
        bedrock_model_id="approved-model",
        database_url="postgresql+psycopg://app@db/edq",
    )
    with pytest.raises(AppError, match="HTTPS API"):
        Authenticator(settings).authenticate(SimpleNamespace(url=SimpleNamespace(scheme="http"), headers={}))


def test_ranger_requires_explicit_policy_revision(settings):
    directory_settings(settings)
    data = json.loads(settings.policy_path.read_text())
    del data["ranger_policy_revision"]
    settings.policy_path.write_text(json.dumps(data))
    with pytest.raises(AppError, match="policy revision"):
        RangerPolicy(settings, Policy(settings.policy_path)).grant(
            Identity(owner="a", username="alice", roles=["lending_analyst"], password=SecretStr("test"))
        )
