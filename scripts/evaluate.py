"""Non-mutating API smoke evaluation. Uses only the labelled synthetic demo identities."""

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    cases = json.loads((Path(__file__).resolve().parents[1] / "fixtures/evaluations.json").read_text())
    cases = {c["id"]: c for c in cases if "mutation" not in c}
    with httpx.Client(base_url=args.url, headers={"X-Dev-User": "alice"}, timeout=30) as client:

        def run(case, sid):
            if case.get("after"):
                previous = run(cases[case["after"]], sid)
            else:
                previous = None
            response = client.post(f"/v1/sessions/{sid}/questions", json={"question": case["question"]})
            response.raise_for_status()
            rid = response.json()["id"]
            for _ in range(180):
                response = client.get(f"/v1/requests/{rid}")
                response.raise_for_status()
                row = response.json()
                if row["status"] not in ("queued", "running"):
                    break
                time.sleep(0.2)
            assert row["status"] == case["expect"], (case["id"], row.get("error"))
            if "rows" in case:
                assert row["result"]["row_count"] == case["rows"]
            if "chart" in case:
                assert row["result"]["chart"]["chart_type"] == case["chart"]
            if "result" in case:
                assert row["result"] == case["result"]
            for key in ("country", "grain"):
                if key in case:
                    assert row["plan"]["state"][key] == case[key]
            if "preserve" in case:
                for key in case["preserve"]:
                    assert row["plan"]["state"][key] == previous["plan"]["state"][key]
            return row

        for case in cases.values():
            session = client.post("/v1/sessions", json={"title": "Synthetic acceptance evaluation"})
            session.raise_for_status()
            row = run(case, session.json()["id"])
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "status": row["status"],
                        "correlation_id": row["correlation_id"],
                        "backend": row["result"]["execution_backend"] if row.get("result") else None,
                    }
                )
            )


if __name__ == "__main__":
    main()
