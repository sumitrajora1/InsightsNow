import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import Authenticator, Policy
from app.config import Settings
from app.contracts import Identity
from app.errors import AppError
from app.execution import ExecutionControl, TrinoExecutor
from app.metadata import OpenMetadataAPI


def test_oidc_verifies_signature_issuer_audience_expiry(settings):
    import time

    settings.auth_mode = "oidc"
    settings.oidc_issuer, settings.oidc_audience = "https://issuer", "edq-api"
    settings.oidc_jwks_url = "https://issuer/jwks"
    auth = Authenticator(settings)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    claims = {
        "iss": "https://issuer",
        "sub": "123",
        "aud": "edq-api",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60,
        "roles": ["lending_analyst"],
        "preferred_username": "alice",
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    request = SimpleNamespace(headers={"Authorization": "Bearer " + token})
    assert auth.authenticate(request).owner == "https://issuer|123"
    for patch in ({"aud": "evil"}, {"iss": "https://evil"}, {"exp": 1}):
        bad = jwt.encode(claims | patch, key, algorithm="RS256")
        with pytest.raises(AppError):
            auth.authenticate(SimpleNamespace(headers={"Authorization": "Bearer " + bad}))


def test_production_cannot_enable_demo_defaults():
    with pytest.raises(ValueError):
        Settings(environment="production")


def test_openmetadata_contract_probe_and_missing_capability(settings):
    grant = Policy(settings.policy_path).grant(
        Identity(owner="a", username="alice", roles=["lending_analyst"])
    )
    fixture = json.loads(settings.metadata_fixture.read_text())
    by_name = dict(zip(grant.source_names.values(), fixture["assets"]))
    settings.om_url, settings.om_expected_version = "https://metadata", "2.0"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("system/version"):
            return httpx.Response(200, json={"version": "2.0.1"})
        from urllib.parse import unquote

        name = unquote(request.url.path.split("/name/")[1])
        asset = by_name[name]
        return httpx.Response(
            200,
            json={
                "id": asset["id"],
                "fullyQualifiedName": name,
                "columns": [{"name": c["name"], "dataType": c["data_type"]} for c in asset["columns"]],
                "extension": {"edqContract": {"metrics": fixture["metrics"], "joins": fixture["joins"]}}
                if asset["id"] == "exposure"
                else {},
                "owners": [],
                "tags": [],
            },
        )

    om = OpenMetadataAPI(
        settings, httpx.Client(base_url="https://metadata", transport=httpx.MockTransport(handler))
    )
    context = om.retrieve("exposure", grant)
    assert not context.synthetic and len(context.metrics) == 1
    assert not any("/search" in x or "mcp" in x or "ontology" in x for x in seen)
    settings.om_expected_version = "1.10"
    with pytest.raises(AppError, match="could not be verified"):
        om.retrieve("exposure", grant)


def test_live_trino_never_falls_back_to_service_identity(settings):
    settings.trino_auth = "jwt_passthrough"
    with pytest.raises(AppError, match="credential"):
        TrinoExecutor(settings).execute(
            "SELECT 1", Identity(owner="a", username="alice", roles=[]), ExecutionControl(1), "c"
        )


def test_bedrock_json_contract_and_bounded_repair(settings):
    from app.llm import BedrockPlanner, MockPlanner
    from app.metadata import FixtureMetadata

    identity = Identity(owner="a", username="alice", roles=["lending_analyst"])
    grant = Policy(settings.policy_path).grant(identity)
    context = FixtureMetadata(settings).retrieve("exposure", grant)
    good = MockPlanner().plan("exposure last six months", context, None, grant)
    bad = good.model_copy(deep=True)
    bad.sql = "DROP TABLE secret.customers"

    class Model:
        def __init__(self):
            self.calls = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            chosen = bad if len(self.calls) == 1 else good
            return {
                "output": {"message": {"content": [{"text": chosen.model_dump_json()}]}},
                "usage": {"inputTokens": 50, "outputTokens": 20},
            }

    model = Model()
    settings.bedrock_model_id = "test-converse-model"
    result = BedrockPlanner(settings, client=model).plan("exposure last six months", context, None, grant)
    assert len(model.calls) == 2 and result.sql == good.sql
    assert "approved_sql_template" in model.calls[1]["messages"][0]["content"][0]["text"]
    assert "toolConfig" not in model.calls[0]  # Model receives no SQL execution tool.


def test_model_invalid_json_fails_after_two_attempts(settings):
    from app.llm import BedrockPlanner
    from app.metadata import FixtureMetadata

    grant = Policy(settings.policy_path).grant(
        Identity(owner="a", username="alice", roles=["lending_analyst"])
    )
    context = FixtureMetadata(settings).retrieve("exposure", grant)

    class Model:
        calls = 0

        def converse(self, **kwargs):
            self.calls += 1
            return {"output": {"message": {"content": [{"text": "not valid JSON"}]}}}

    model = Model()
    with pytest.raises(AppError, match="No query ran"):
        BedrockPlanner(settings, client=model).plan("exposure", context, None, grant)
    assert model.calls == 2
