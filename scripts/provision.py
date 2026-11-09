"""Compose-only synthetic bootstrap. The API never receives the administrator credential."""

import os
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql

host = os.environ.get("PGHOST", "postgres")
port = int(os.environ.get("PGPORT", "5432"))
admin_password = os.environ["POSTGRES_PASSWORD"]
admin_args = dict(host=host, port=port, user="edq_admin", password=admin_password)
with psycopg.connect(**admin_args, dbname="edq", autocommit=True) as db:
    for role, key in (("edq_app", "APP_DB_PASSWORD"), ("edq_reader", "SOURCE_DB_PASSWORD")):
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            db.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(os.environ[key])
                )
            )
    if not db.execute("SELECT 1 FROM pg_database WHERE datname='synthetic'").fetchone():
        db.execute("CREATE DATABASE synthetic")
    db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
os.environ["EDQ_DATABASE_URL"] = f"postgresql+psycopg://edq_admin:{admin_password}@{host}:{port}/edq"
command.upgrade(Config("alembic.ini"), "head")
with psycopg.connect(**admin_args, dbname="edq", autocommit=True) as db:
    db.execute("GRANT USAGE ON SCHEMA public TO edq_app")
    db.execute("GRANT SELECT, INSERT, UPDATE ON conversations, query_requests TO edq_app")
    db.execute("GRANT SELECT, INSERT ON audit_events TO edq_app")
    db.execute("GRANT USAGE, SELECT ON SEQUENCE audit_events_id_seq TO edq_app")
    # Runtime cannot UPDATE or DELETE audit rows. Retention uses the maintenance role separately.
with psycopg.connect(**admin_args, dbname="synthetic", autocommit=True) as db:
    if not db.execute("SELECT to_regclass('lending.facilities')").fetchone()[0]:
        db.execute(Path("fixtures/synthetic.sql").read_text())
    db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    db.execute("GRANT USAGE ON SCHEMA lending TO edq_reader")
    db.execute("GRANT SELECT ON ALL TABLES IN SCHEMA lending TO edq_reader")
print("Synthetic source seeded; migrations applied; separate least-privilege roles granted.")
