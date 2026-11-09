"""Generate reproducible SYNTHETIC fixtures. Never point this script at bank data."""

import calendar
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def records():
    facilities, snapshots = [], []
    for i, (industry, country, currency) in enumerate(
        (a, b, c)
        for a in ("Manufacturing", "Trade", "Real estate")
        for b in ("UAE", "UK", "KSA")
        for c in ("AED", "USD")
        for _ in range(2)
    ):
        fid = f"SYN-{i + 1:03}"
        facilities.append((fid, industry, country))
        for m in range(12):
            year, month = divmod(2025 * 12 + 10 + m, 12)
            month += 1
            end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
            snapshots.append((fid, end, 100000 + i * 10000 + m * 1000, currency))
    return facilities, snapshots


def seed_sqlite(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS facilities (
            facility_id TEXT PRIMARY KEY, industry TEXT NOT NULL, country TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS exposure_snapshots (
            facility_id TEXT NOT NULL REFERENCES facilities(facility_id), snapshot_date TEXT NOT NULL,
            exposure_amount NUMERIC NOT NULL, currency TEXT NOT NULL,
            PRIMARY KEY(facility_id, snapshot_date));
          DELETE FROM exposure_snapshots; DELETE FROM facilities;
        """)
        facilities, snapshots = records()
        db.executemany("INSERT INTO facilities VALUES (?,?,?)", facilities)
        db.executemany("INSERT INTO exposure_snapshots VALUES (?,?,?,?)", snapshots)


def write_fixtures():
    h, o = "demo_hive.lending.exposure_snapshots", "demo_oracle.lending.facilities"

    def asset(id, fqn, columns):
        return {
            "id": id,
            "fqn": fqn,
            "columns": [
                {"name": n, "data_type": t, "description": d, "sensitivity": "internal"}
                for n, t, d in columns
            ],
            "description": "SYNTHETIC demonstration data, not live bank records.",
            "owner": "Synthetic Lending Data Owner",
            "source_url": f"fixture://{id}",
            "lineage": [],
            "version": "synthetic-v1",
        }

    data = {
        "synthetic": True,
        "capabilities": ["approved_contract", "glossary", "columns", "documented_join"],
        "assets": [
            asset(
                "exposure",
                h,
                [
                    ("facility_id", "varchar", "Facility foreign key"),
                    ("snapshot_date", "date", "Complete month-end snapshot date"),
                    ("exposure_amount", "decimal(18,2)", "Outstanding balance in recorded currency"),
                    ("currency", "varchar", "ISO currency, never mix currencies in a sum"),
                ],
            ),
            asset(
                "facilities",
                o,
                [
                    ("facility_id", "varchar", "Unique facility key"),
                    ("industry", "varchar", "Approved industry grouping"),
                    ("country", "varchar", "Facility country"),
                ],
            ),
        ],
        "metrics": [
            {
                "id": "month_end_exposure",
                "name": "Corporate lending exposure",
                "definition": "Outstanding funded balance at each complete month end. Sum facilities within one date and one currency only. A period comparison uses the average of complete monthly totals, not the sum of snapshot balances.",
                "source_asset": "exposure",
                "value_column": "exposure_amount",
                "snapshot_column": "snapshot_date",
                "currency_column": "currency",
                "default_currency": "AED",
                "units": "currency units",
                "grain": "one facility per month end",
                "as_of": "2026-10-31",
                "join_id": "facility_join",
                "formula": "sum_per_snapshot",
                "period_comparison": "average_month_end",
                "glossary_ref": "glossary://CorporateLending.Exposure",
                "minimum_group_size": 2,
            }
        ],
        "joins": [
            {
                "id": "facility_join",
                "left_asset": "exposure",
                "left_column": "facility_id",
                "right_asset": "facilities",
                "right_column": "facility_id",
                "cardinality": "many_to_one",
            }
        ],
        "available_definitions": {
            "CorporateLending.Exposure": "Outstanding funded balance at a complete month end; excludes undrawn commitments."
        },
    }
    (ROOT / "fixtures/metadata.json").write_text(json.dumps(data, indent=2))
    grants = {
        h: [x["name"] for x in data["assets"][0]["columns"]],
        o: [x["name"] for x in data["assets"][1]["columns"]],
    }
    (ROOT / "config/policy.json").write_text(
        json.dumps(
            {
                "openmetadata_names": {
                    h: "SyntheticHive.demo.lending.exposure_snapshots",
                    o: "SyntheticOracle.demo.lending.facilities",
                },
                "roles": {
                    "lending_analyst": {"assets": grants, "currencies": ["AED", "USD"], "country": None},
                    "uae_analyst": {"assets": grants, "currencies": ["AED"], "country": "UAE"},
                },
            },
            indent=2,
        )
    )
    facilities, snapshots = records()

    def quote(x):
        return "'" + str(x).replace("'", "''") + "'"

    sql = """-- SYNTHETIC data only. Credentials and grants are provisioned separately.
CREATE SCHEMA IF NOT EXISTS lending;
CREATE TABLE lending.facilities (facility_id varchar PRIMARY KEY, industry varchar NOT NULL, country varchar NOT NULL);
CREATE TABLE lending.exposure_snapshots (facility_id varchar NOT NULL REFERENCES lending.facilities,
 snapshot_date date NOT NULL, exposure_amount decimal(18,2) NOT NULL, currency varchar NOT NULL,
 PRIMARY KEY (facility_id, snapshot_date));
"""
    for table, rows in [("facilities", facilities), ("exposure_snapshots", snapshots)]:
        sql += (
            f"INSERT INTO lending.{table} VALUES\n"
            + ",\n".join("(" + ",".join(quote(x) for x in row) + ")" for row in rows)
            + ";\n"
        )
    (ROOT / "fixtures/synthetic.sql").write_text(sql)


if __name__ == "__main__":
    write_fixtures()
    seed_sqlite(ROOT / ".runtime/lending.db")
    print("Seeded explicitly synthetic metadata and 12 complete months of lending snapshots.")
