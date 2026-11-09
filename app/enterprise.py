"""LDAPS at Trino's password authenticator; per-user Ranger authorization at Trino.

No Ranger Admin policy emulation, service-account execution or persisted passwords.
The admin-maintained registry narrows discovery; engine probes can only remove grants.
"""

import json
import re
import threading
import time
from collections import deque

from pydantic import SecretStr
from trino.auth import BasicAuthentication, JWTAuthentication
from trino.dbapi import connect
from trino.exceptions import HttpError, TrinoUserError

from app.contracts import Identity
from app.errors import AppError

USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LoginGuard:
    """Bounded single-worker failure limiter. Add gateway/distributed limits before scaling."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.failures = deque(maxlen=1000)

    def check(self, username):
        now = time.monotonic()
        with self.lock:
            while self.failures and self.failures[0][0] < now - self.settings.login_failure_window_seconds:
                self.failures.popleft()
            if (
                len(self.failures) >= 100
                or sum(u == username for _, u in self.failures) >= self.settings.login_failure_limit
            ):
                raise AppError("login_throttled", "Sign-in is temporarily limited. Try again later.", 429)

    def failed(self, username):
        with self.lock:
            self.failures.append((time.monotonic(), username))


def connection(settings, username, auth, source):
    if settings.trino_scheme != "https":
        raise AppError("identity_unavailable", "Enterprise authentication requires TLS.", 503)
    return connect(
        host=settings.trino_host,
        port=settings.trino_port,
        http_scheme="https",
        user=username,
        auth=auth,
        verify=settings.trino_ca_bundle or True,
        max_attempts=1,
        request_timeout=settings.dependency_timeout_seconds,
        source=source,
        session_properties={"query_max_run_time": f"{max(1, int(settings.dependency_timeout_seconds))}s"},
    )


def close(db, cursor):
    if cursor:
        try:
            cursor.cancel()
        except Exception:
            pass
    if db:
        try:
            db.close()
        except Exception:
            pass


def authenticate_password(settings, username, password, guard):
    if not USERNAME.fullmatch(username) or not password or len(password) > 1024:
        raise AppError("unauthenticated", "Directory sign-in failed.", 401)
    guard.check(username)
    db = cursor = None
    try:
        db = connection(settings, username, BasicAuthentication(username, password), "edq-directory-signin")
        cursor = db.cursor()
        cursor.execute("SELECT current_user")
        rows = cursor.fetchmany(2)
        # Avoid aliases/impersonation ambiguity. Configure canonical short-name LDAP bind mapping.
        if len(rows) != 1 or list(rows[0]) != [username]:
            raise AppError("unauthenticated", "Directory identity mapping could not be verified.", 401)
    except Exception as exc:
        guard.failed(username)
        if isinstance(exc, AppError):
            raise
        if isinstance(exc, HttpError) and re.match(r"error (401|403)\b", str(exc)):
            raise AppError("unauthenticated", "Directory sign-in failed.", 401) from exc
        if isinstance(exc, TrinoUserError) and exc.error_name == "PERMISSION_DENIED":
            raise AppError(
                "access_denied", "Directory user has no approved Trino query access.", 403
            ) from exc
        raise AppError("identity_unavailable", "Directory/Trino sign-in could not be verified.", 503) from exc
    finally:
        close(db, cursor)
    # Explicit canonical-user registry controls app discovery only. Ranger remains the authority at Trino.
    try:
        registry = json.loads(settings.policy_path.read_text())
        roles = registry.get("users", {}).get(username, [])
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise ValueError("Invalid role registry")
    except Exception as exc:
        raise AppError("identity_unavailable", "The discovery registry is unavailable.", 503) from exc
    return Identity(
        owner=f"ldaps:{settings.ldaps_identity_namespace}:{username}",
        username=username,
        roles=roles,
        password=SecretStr(password),
    )


class RangerPolicy:
    """Intersect the domain registry with fresh user-authenticated engine authorization.

    A zero-row SELECT references every approved column in each asset. If any column
    is denied, omit the entire asset: deliberately conservative, no partial guessing.
    Row filters/masks are applied by Ranger again on real query execution.
    """

    def __init__(self, settings, registry):
        self.settings, self.registry = settings, registry

    def grant(self, identity):
        grant = self.registry.grant(identity)
        if (
            not isinstance(grant.authorization_revision, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", grant.authorization_revision)
            or grant.authorization_revision.startswith("REPLACE_")
        ):
            raise AppError(
                "authorization_unavailable", "An approved Ranger policy revision is required.", 503
            )
        if len(grant.columns) > 8 or any(len(cols) > 64 for cols in grant.columns.values()):
            raise AppError("authorization_unavailable", "Narrow the approved discovery registry.", 503)
        cfg = self.settings
        if cfg.trino_auth == "ldap_password" and identity.password.get_secret_value():
            auth = BasicAuthentication(identity.username, identity.password.get_secret_value())
        elif cfg.trino_auth == "jwt_passthrough" and identity.token:
            auth = JWTAuthentication(identity.token)
        else:
            raise AppError("identity_unavailable", "Per-user engine credentials are required.", 403)
        allowed = {}
        deadline = time.monotonic() + cfg.dependency_timeout_seconds
        db = cursor = None
        try:
            db = connection(cfg, identity.username, auth, "edq-ranger-discovery")
            cursor = db.cursor()
            for fqn, columns in grant.columns.items():
                if time.monotonic() >= deadline:
                    raise AppError(
                        "authorization_unavailable", "Authorization checks exceeded their time budget.", 503
                    )
                parts = fqn.split(".")
                if (
                    len(parts) != 3
                    or not columns
                    or not all(IDENTIFIER.fullmatch(n) for n in [*parts, *columns])
                ):
                    raise AppError(
                        "authorization_unavailable", "Invalid approved discovery identifiers.", 503
                    )
                table = ".".join(f'"{part}"' for part in parts)
                projection = ", ".join(f'"{col}"' for col in sorted(columns))
                try:
                    cursor.execute(f"SELECT {projection} FROM {table} WHERE FALSE LIMIT 0")
                    if cursor.fetchmany(1):
                        raise AppError(
                            "authorization_unavailable", "Unexpected authorization probe data.", 503
                        )
                    allowed[fqn] = columns
                except TrinoUserError as exc:
                    if exc.error_name not in ("PERMISSION_DENIED", "ACCESS_DENIED"):
                        raise
                    # A denied asset never reaches metadata retrieval or the model.
            if time.monotonic() >= deadline:
                raise AppError(
                    "authorization_unavailable", "Authorization checks exceeded their time budget.", 503
                )
            if not allowed:
                raise AppError("access_denied", "Ranger denied the approved discovery assets.", 403)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "authorization_unavailable", "Engine authorization could not be verified.", 503
            ) from exc
        finally:
            close(db, cursor)
        grant.columns = allowed
        grant.source_names = {k: v for k, v in grant.source_names.items() if k in allowed}
        return grant
