import pytest
import sqlglot
from sqlglot import exp

from app.auth import Policy
from app.contracts import Identity
from app.errors import AppError
from app.llm import MockPlanner
from app.metadata import FixtureMetadata
from app.validation import SQLValidator


@pytest.fixture
def domain(settings):
    user = Identity(owner="alice", username="alice", roles=["lending_analyst"])
    grant = Policy(settings.policy_path).grant(user)
    return FixtureMetadata(settings).retrieve("exposure", grant), grant


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE demo_hive.lending.exposure_snapshots",
        "SELECT 1; DELETE FROM x",
        "CALL system.runtime.kill_query('x','x')",
        "SHOW CATALOGS",
        "SELECT * FROM demo_hive.lending.exposure_snapshots",
        "SELECT currency FROM evil.lending.exposure_snapshots",
        "SELECT secret FROM demo_hive.lending.exposure_snapshots",
        "SELECT s.exposure_amount FROM demo_hive.lending.exposure_snapshots s WHERE s.secret='x'",
        "SELECT http_get('https://evil')",
        "SELECT * FROM TABLE(demo_oracle.system.query(query=>'DROP TABLE x'))",
        "WITH x AS (DELETE FROM demo_hive.lending.exposure_snapshots) SELECT 1",
        "WITH RECURSIVE x AS (SELECT 1) SELECT 1",
        "SELECT sum(exposure_amount) OVER () FROM demo_hive.lending.exposure_snapshots",
        "SELECT currency FROM demo_hive.lending.exposure_snapshots UNION SELECT country FROM demo_oracle.lending.facilities",
    ],
)
def test_unsafe_or_unknown_sql_rejected(domain, sql):
    context, grant = domain
    with pytest.raises(AppError, match="failed deterministic"):
        SQLValidator(10).validate(sql, {}, context, grant)


def test_safe_cte_and_ast_row_bound(domain):
    context, grant = domain
    sql = "WITH x AS (SELECT currency FROM demo_hive.lending.exposure_snapshots) SELECT currency FROM x"
    result = SQLValidator(3).validate(sql, {}, context, grant)
    assert sqlglot.parse_one(result, read="trino").args["limit"].expression.this == "4"


def test_literal_binding_cannot_inject_sql(domain):
    context, grant = domain
    sql = "SELECT currency FROM demo_hive.lending.exposure_snapshots WHERE currency=:currency"
    result = SQLValidator(3).validate(sql, {"currency": "AED'; DROP TABLE facilities; --"}, context, grant)
    assert len(sqlglot.parse(result, read="trino")) == 1
    assert any("DROP TABLE" in n.this for n in sqlglot.parse_one(result, read="trino").find_all(exp.Literal))


def test_semantic_guard_rejects_wrong_balance_aggregation(domain):
    context, grant = domain
    plan = MockPlanner().plan("exposure last six months", context, None, grant)
    unsafe = "SELECT SUM(exposure_amount) FROM demo_hive.lending.exposure_snapshots"
    with pytest.raises(AppError):
        SQLValidator(100).validate(unsafe, {}, context, grant, plan)


def test_denied_column_removed_from_predicates(domain):
    context, grant = domain
    fqn = "demo_oracle.lending.facilities"
    grant.columns[fqn].remove("country")
    with pytest.raises(AppError):
        SQLValidator(100).validate(f"SELECT industry FROM {fqn} WHERE country='UK'", {}, context, grant)
