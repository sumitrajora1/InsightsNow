"""Opt-in tests of a running synthetic stack, not enterprise-source certification."""

import os
import time
from decimal import Decimal

import httpx
import pytest
from trino.dbapi import connect
from trino.exceptions import TrinoUserError

pytestmark = pytest.mark.skipif(
    os.environ.get("EDQ_RUN_TRINO_TESTS") != "1", reason="Requires running synthetic Trino stack"
)


def test_real_stack_result_identity_chart_and_audit():
    url = os.environ.get("EDQ_TEST_API_URL", "http://127.0.0.1:8000")
    with httpx.Client(base_url=url, headers={"X-Dev-User": "alice"}, timeout=30) as c:
        sid = c.post("/v1/sessions", json={}).json()["id"]

        def ask(question):
            response = c.post(f"/v1/sessions/{sid}/questions", json={"question": question})
            response.raise_for_status()
            rid = response.json()["id"]
            for _ in range(300):
                row = c.get(f"/v1/requests/{rid}").json()
                if row["status"] not in ("queued", "running"):
                    break
                time.sleep(0.1)
            assert row["status"] == "completed", row.get("error")
            return row

        first = ask("Show corporate lending exposure by industry for the last six months.")
        assert first["result"]["execution_backend"] == "trino"
        assert first["result"]["row_count"] == 18
        assert Decimal(str(first["result"]["rows"][0][-1])) == 906000
        assert first["result"]["chart"]["chart_type"] == "line"
        scoped = ask("Only show UAE")
        assert Decimal(str(scoped["result"]["rows"][0][-1])) == 222000
        assert scoped["plan"]["state"]["months"] == first["plan"]["state"]["months"]
        rid = scoped["id"]
        qid = scoped["result"]["query_id"]
        assert not qid.startswith("sqlite") and qid != "unreported"
        events = c.get(f"/v1/requests/{rid}/audit").json()
        assert any(e["details"].get("query_id") == qid for e in events)
        assert {e["correlation_id"] for e in events} == {scoped["correlation_id"]}
        assert c.get(f"/v1/requests/{rid}", headers={"X-Dev-User": "bob"}).status_code == 404
        assert c.post(f"/v1/requests/{rid}/chart", json={"chart_type": "bar"}).status_code == 200
        assert c.get(f"/v1/requests/{rid}").json()["result"]["query_id"] == qid


def query(user, sql):
    with connect(
        host=os.environ.get("EDQ_TEST_TRINO_HOST", "127.0.0.1"),
        port=int(os.environ.get("EDQ_TEST_TRINO_PORT", "8080")),
        user=user,
        request_timeout=20,
        max_attempts=1,
    ) as db:
        cursor = db.cursor()
        cursor.execute(sql)
        return cursor.fetchall()


def test_engine_row_filters_apply_without_application():
    assert query("bob", "SELECT DISTINCT country FROM demo_oracle.lending.facilities") == [["UAE"]]
    assert query("bob", "SELECT DISTINCT currency FROM demo_hive.lending.exposure_snapshots") == [["AED"]]
    assert query("bob", "SELECT COUNT(*) FROM demo_hive.lending.exposure_snapshots") == [[72]]


def test_engine_denies_unapproved_identity_and_ddl():
    with pytest.raises(TrinoUserError):
        query("charlie", "SELECT * FROM demo_oracle.lending.facilities")
    with pytest.raises(TrinoUserError):
        query("alice", "CREATE TABLE demo_oracle.lending.must_never_exist (x BIGINT)")
