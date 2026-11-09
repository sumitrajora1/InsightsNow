import base64
import binascii
import json
from dataclasses import dataclass

import jwt
from fastapi import Request

from app.config import Settings
from app.contracts import Identity
from app.errors import AppError

DEV_USERS = {
    "alice": ["lending_analyst"],
    "bob": ["uae_analyst"],
    "charlie": ["no_access"],
}


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        from app.enterprise import LoginGuard

        self.login_guard = LoginGuard(settings)
        self.jwks = (
            jwt.PyJWKClient(settings.oidc_jwks_url, timeout=settings.dependency_timeout_seconds)
            if settings.auth_mode == "oidc"
            else None
        )

    def authenticate(self, request: Request) -> Identity:
        if self.settings.auth_mode == "trino_ldaps":
            if self.settings.environment == "production" and request.url.scheme != "https":
                raise AppError("unauthenticated", "Directory sign-in requires an HTTPS API connection.", 401)
            from app.enterprise import authenticate_password

            raw = request.headers.get("Authorization", "")
            try:
                if not raw.startswith("Basic ") or len(raw) > 4096:
                    raise ValueError("Basic credentials required")
                username, password = base64.b64decode(raw[6:], validate=True).decode("utf-8").split(":", 1)
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise AppError("unauthenticated", "Directory sign-in is required.", 401) from exc
            return authenticate_password(self.settings, username, password, self.login_guard)
        if self.settings.auth_mode == "development":
            name = request.headers.get("X-Dev-User", "")
            if name not in DEV_USERS:
                raise AppError("unauthenticated", "Select an explicitly labelled development user.", 401)
            return Identity(owner=f"development:{name}", username=name, roles=DEV_USERS[name])
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise AppError("unauthenticated", "Enterprise sign-in is required.", 401)
        token = auth[7:]
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                issuer=self.settings.oidc_issuer,
                audience=self.settings.oidc_audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
            roles = claims.get(self.settings.oidc_roles_claim, [])
            name = claims.get(self.settings.oidc_username_claim, claims["sub"])
            if not isinstance(roles, list) or not all(isinstance(x, str) for x in roles):
                raise ValueError("Invalid roles")
            if not isinstance(name, str) or not name or len(name) > 200:
                raise ValueError("Invalid username")
            return Identity(owner=f"{claims['iss']}|{claims['sub']}", username=name, roles=roles, token=token)
        except Exception as exc:
            raise AppError("unauthenticated", "Sign-in expired or could not be verified.", 401) from exc


@dataclass
class Grant:
    columns: dict[str, set[str]]
    country: str | None
    currencies: set[str]
    source_names: dict[str, str]
    authorization_revision: str = ""


class Policy:
    """Deployment-admin policy. Reload each call; never obtain permissions from the LLM."""

    def __init__(self, path):
        self.path = path

    def grant(self, identity: Identity) -> Grant:
        data = json.loads(self.path.read_text())
        rules = [data["roles"][r] for r in identity.roles if r in data["roles"]]
        if not rules:
            raise AppError("access_denied", "No authorized lending assets are available.", 403)
        # Multiple roles require consistent row restrictions: fail closed instead of unioning accidentally.
        countries = {r.get("country") for r in rules}
        if len(countries) > 1:
            raise AppError("access_denied", "Conflicting role constraints require administrator review.", 403)
        columns: dict[str, set[str]] = {}
        for rule in rules:
            for fqn, names in rule["assets"].items():
                columns.setdefault(fqn, set()).update(names)
        return Grant(
            columns,
            next(iter(countries)),
            set.intersection(*(set(r["currencies"]) for r in rules)),
            data["openmetadata_names"],
            data.get("ranger_policy_revision", ""),
        )
