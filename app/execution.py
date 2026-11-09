import json
import sqlite3
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

import jwt
import sqlglot
from sqlglot import exp
from trino.auth import BasicAuthentication, JWTAuthentication
from trino.dbapi import connect

from app.config import Settings
from app.contracts import Identity, QueryResult, ResultColumn
from app.errors import AppError


class ExecutionControl:
    def __init__(self, timeout: float):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.lock = threading.Lock()
        self.callback = None
        self.query_id = None

    def attach(self, callback):
        with self.lock:
            self.callback = callback
        if self.cancelled.is_set():
            callback()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            callback = self.callback
        if callback:
            try:
                callback()
            except Exception:
                pass

    def check(self):
        if self.cancelled.is_set():
            raise AppError("cancelled", "The request was cancelled.", 409)
        if time.monotonic() >= self.deadline:
            raise AppError("timeout", "The request exceeded its time limit.", 408)


class Executor(Protocol):
    def execute(
        self, sql: str, identity: Identity, control: ExecutionControl, correlation_id: str
    ) -> QueryResult: ...
    def health(self) -> bool: ...


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)  # Preserve decimal precision on the wire.
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise AppError("unsupported_result", "The result contains an unsupported nested value.", 422)


def bound_rows(rows, settings):
    output, total, truncated = [], 0, False
    for row in rows:
        if len(output) >= settings.max_rows:
            truncated = True
            break
        values = [json_value(v) for v in row]
        if any(isinstance(v, str) and len(v) > settings.max_cell_chars for v in values):
            raise AppError("result_too_large", "A result value exceeds the configured safety bound.", 422)
        total += len(json.dumps(values).encode())
        if total > settings.max_result_bytes:
            truncated = True
            break
        output.append(values)
    return output, truncated


class SQLiteDemoExecutor:
    """Local offline SQL execution only. NOT Trino, CDP or Oracle. Label in every response."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def execute(self, sql, identity, control, correlation_id):
        start = time.monotonic()
        control.check()
        tree = sqlglot.parse_one(sql, read="trino")
        tree = tree.transform(
            lambda n: (
                exp.Table(this=n.this, alias=n.args.get("alias"))
                if isinstance(n, exp.Table) and n.catalog
                else n
            )
        )
        db = sqlite3.connect(f"file:{self.settings.demo_data_path}?mode=ro", uri=True)
        control.attach(db.interrupt)
        db.set_progress_handler(
            lambda: int(control.cancelled.is_set() or time.monotonic() >= control.deadline), 100
        )
        try:
            cur = db.execute(tree.sql(dialect="sqlite"))
            raw = cur.fetchmany(self.settings.max_rows + 1)
            control.check()
            rows, truncated = bound_rows(raw, self.settings)
            cols = [
                ResultColumn(
                    name=d[0],
                    data_type=(
                        "date"
                        if d[0] == "snapshot_date"
                        else "decimal"
                        if "exposure" in d[0]
                        else "integer"
                        if d[0] == "months_observed"
                        else "varchar"
                    ),
                )
                for d in cur.description
            ]
            return QueryResult(
                columns=cols,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                query_id="sqlite-demo-" + str(uuid4()),
                duration_ms=(time.monotonic() - start) * 1000,
                synthetic=True,
                execution_backend="sqlite_demo",
            )
        except AppError:
            raise
        except Exception as exc:
            control.check()
            raise AppError("execution_failed", "The synthetic local query failed.", 502) from exc
        finally:
            db.close()

    def health(self):
        return self.settings.demo_data_path.is_file()


class TrinoExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def execute(self, sql, identity, control, correlation_id):
        cfg = self.settings
        auth = None
        if cfg.trino_auth == "jwt_passthrough":
            if not identity.token or cfg.trino_scheme != "https" or not cfg.trino_token_audience:
                raise AppError(
                    "identity_unavailable", "A verified user credential accepted by Trino is required.", 403
                )
            # Token was signature-verified at API entry. Check that forwarding was explicitly provisioned.
            claims = jwt.decode(identity.token, options={"verify_signature": False})
            aud = claims.get("aud", [])
            if cfg.trino_token_audience not in ([aud] if isinstance(aud, str) else aud):
                raise AppError("identity_unavailable", "Your credential is not provisioned for Trino.", 403)
            if claims.get("exp", 0) <= time.time():
                raise AppError("unauthenticated", "Sign-in expired before execution.", 401)
            auth = JWTAuthentication(identity.token)
        elif cfg.trino_auth == "ldap_password":
            if (
                cfg.auth_mode != "trino_ldaps"
                or cfg.trino_scheme != "https"
                or not identity.password.get_secret_value()
            ):
                raise AppError("identity_unavailable", "Verified directory credentials are required.", 403)
            auth = BasicAuthentication(identity.username, identity.password.get_secret_value())
        elif cfg.environment == "production":
            raise AppError("identity_unavailable", "Development query identity is disabled.", 403)
        start = time.monotonic()
        # No fallback account. Trino must independently authenticate and authorize this identity.
        conn = connect(
            host=cfg.trino_host,
            port=cfg.trino_port,
            http_scheme=cfg.trino_scheme,
            user=identity.username,
            auth=auth,
            source="english-data-query",
            verify=cfg.trino_ca_bundle or True,
            max_attempts=1,
            request_timeout=min(cfg.dependency_timeout_seconds, cfg.query_timeout_seconds),
            client_tags=["edq"],
            http_headers={"X-Trino-Client-Info": json.dumps({"correlation_id": correlation_id})},
            session_properties={"query_max_run_time": f"{int(cfg.query_timeout_seconds)}s"},
        )
        cur = conn.cursor()
        control.attach(cur.cancel)
        control.query_id = lambda: cur.query_id
        timer = threading.Timer(max(0.01, control.deadline - time.monotonic()), control.cancel)
        timer.daemon = True
        timer.start()
        try:
            control.check()
            cur.execute(sql)
            raw = cur.fetchmany(cfg.max_rows + 1)
            if time.monotonic() >= control.deadline:
                raise AppError("timeout", "The query exceeded its time limit.", 408)
            control.check()
            rows, truncated = bound_rows(raw, cfg)
            cols = [ResultColumn(name=d[0], data_type=str(d[1])) for d in cur.description]
            return QueryResult(
                columns=cols,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                query_id=cur.query_id or "unreported",
                duration_ms=(time.monotonic() - start) * 1000,
                synthetic=cfg.environment != "production" and cfg.metadata_provider == "fixture",
                execution_backend="trino",
            )
        except AppError:
            raise
        except Exception as exc:
            if time.monotonic() >= control.deadline:
                raise AppError("timeout", "The query exceeded its time limit.", 408) from exc
            control.check()
            raise AppError(
                "execution_unavailable", "Trino could not complete this authorized request.", 503
            ) from exc
        finally:
            timer.cancel()
            try:
                cur.cancel()  # Release server resources even after a bounded partial read.
            except Exception:
                pass  # Cleanup must not replace the original result or error.
            finally:
                conn.close()

    def health(self):
        import httpx

        try:
            response = httpx.get(
                f"{self.settings.trino_scheme}://{self.settings.trino_host}:{self.settings.trino_port}/v1/info",
                timeout=3,
                verify=self.settings.trino_ca_bundle or True,
            )
            return response.status_code == 200
        except Exception:
            return False
