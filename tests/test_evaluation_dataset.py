import json

import pytest

from app.config import ROOT
from tests.conftest import ask, session

CASES = json.loads((ROOT / "fixtures/evaluations.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_expected_behaviour_dataset(client, settings, case):
    if "mutation" in case:
        data = json.loads(settings.metadata_fixture.read_text())
        data["metrics" if case["mutation"] == "remove_metrics" else "joins"] = []
        settings.metadata_fixture.write_text(json.dumps(data))
    sid = session(client)

    def run(current):
        previous = run(BY_ID[current["after"]]) if current.get("after") else None
        row = ask(client, sid, current["question"])
        assert row["status"] == current["expect"], row
        if "rows" in current:
            assert row["result"]["row_count"] == current["rows"]
        if "chart" in current:
            assert row["result"]["chart"]["chart_type"] == current["chart"]
        if "result" in current:
            assert row["result"] == current["result"]
        for key in ("country", "grain"):
            if key in current:
                assert row["plan"]["state"][key] == current[key]
        for key in current.get("preserve", []):
            assert row["plan"]["state"][key] == previous["plan"]["state"][key]
        return row

    run(case)
