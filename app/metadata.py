import json
from typing import Protocol
from urllib.parse import quote

import httpx

from app.auth import Grant
from app.config import Settings
from app.contracts import Asset, Column, MetadataContext
from app.errors import AppError


class MetadataProvider(Protocol):
    def retrieve(self, question: str, grant: Grant) -> MetadataContext: ...
    def health(self) -> bool: ...


def restrict(context: MetadataContext, grant: Grant, max_chars: int) -> MetadataContext:
    """Filter before model exposure and response rendering; no process-wide metadata cache."""
    assets = []
    for asset in context.assets:
        if asset.fqn in grant.columns:
            allowed = grant.columns[asset.fqn]
            assets.append(
                asset.model_copy(
                    update={
                        "columns": [c for c in asset.columns if c.name in allowed],
                        "lineage": [r for r in asset.lineage if r in grant.columns],
                        "description": asset.description[:1000],
                    }
                )
            )
    ids = {a.id for a in assets}
    columns = {a.id: {c.name for c in a.columns} for a in assets}
    joins = [
        j
        for j in context.joins
        if j.left_asset in ids
        and j.right_asset in ids
        and j.left_column in columns[j.left_asset]
        and j.right_column in columns[j.right_asset]
    ]
    join_ids = {j.id for j in joins}
    metrics = [
        m
        for m in context.metrics
        if m.source_asset in ids
        and m.join_id in join_ids
        and {m.value_column, m.snapshot_column, m.currency_column} <= columns[m.source_asset]
    ]
    approved_terms = {m.glossary_ref.split("://")[-1] for m in metrics}
    result = context.model_copy(
        update={
            "assets": assets,
            "joins": joins,
            "metrics": metrics,
            "available_definitions": {
                k: v for k, v in context.available_definitions.items() if k in approved_terms
            },
        }
    )
    if not assets:
        raise AppError("access_denied", "No authorized metadata is available.", 403)
    if len(result.model_dump_json()) > max_chars:
        raise AppError("metadata_too_large", "Narrow the approved domain metadata before querying.", 422)
    return result


class FixtureMetadata:
    def __init__(self, settings: Settings):
        self.settings = settings

    def retrieve(self, question: str, grant: Grant) -> MetadataContext:
        try:
            raw = MetadataContext.model_validate_json(self.settings.metadata_fixture.read_text())
            return restrict(raw, grant, self.settings.max_metadata_chars)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "metadata_unavailable", "Synthetic metadata is unavailable or invalid.", 503
            ) from exc

    def health(self):
        return self.settings.metadata_fixture.is_file()


class OpenMetadataAPI:
    """Read only pinned REST surface; no ontology/MCP/SDK assumptions.

    Version + response-shape checks fail closed. A bounded, deployment-approved domain
    registry restricts discovery before any service-token metadata request.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(
            base_url=settings.om_url.rstrip("/"),
            timeout=settings.dependency_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.om_token.get_secret_value()}"},
            follow_redirects=False,
        )

    def _get(self, path, params=None):
        r = self.client.get(self.settings.om_api_prefix + path, params=params)
        r.raise_for_status()
        if len(r.content) > 500_000:
            raise ValueError("Unbounded metadata response")
        return r.json()

    def _sensitivity(self, tags):
        levels = [
            self.settings.om_classification_map[t.get("tagFQN", "")]
            for t in tags
            if t.get("tagFQN") in self.settings.om_classification_map
        ]
        if not levels:
            return "unclassified"
        return (
            "restricted"
            if any(v not in ("public", "internal") for v in levels)
            else "internal"
            if "internal" in levels
            else "public"
        )

    def verify_version(self):
        data = self._get("/system/version")
        version = data.get("version", "") if isinstance(data, dict) else str(data)
        if not (
            version == self.settings.om_expected_version
            or version.startswith(self.settings.om_expected_version + ".")
        ):
            raise ValueError("OpenMetadata version differs from the explicitly tested version prefix")
        return version

    def retrieve(self, question: str, grant: Grant):
        try:
            version = self.verify_version()
            assets, contracts, definitions = [], [], {}
            # Domain registry is deliberately small. Never search the global catalog with a broad bot token.
            if len(grant.columns) > 8:
                raise ValueError("Narrow this domain registry to at most eight assets")
            for sql_fqn, allowed in grant.columns.items():
                om_name = grant.source_names[sql_fqn]
                raw = self._get(
                    "/tables/name/" + quote(om_name, safe=""),
                    {"fields": self.settings.om_table_fields},
                )
                if raw.get("fullyQualifiedName") != om_name or raw.get("deleted"):
                    raise ValueError("Metadata asset mismatch")
                columns = [
                    Column(
                        name=c["name"],
                        data_type=c["dataType"],
                        description=(c.get("description") or "")[:600],
                        sensitivity=self._sensitivity(c.get("tags", [])),
                    )
                    for c in raw["columns"]
                    if c["name"] in allowed
                ]
                owners = raw.get("owners") or ([raw["owner"]] if raw.get("owner") else [])
                asset = Asset(
                    id=raw["id"],
                    fqn=sql_fqn,
                    columns=columns,
                    description=(raw.get("description") or "")[:1000],
                    owner=", ".join(o.get("name", "") for o in owners)[:300],
                    version=str(raw.get("version", "")),
                    sensitivity=self._sensitivity(raw.get("tags", [])),
                    source_url=self.settings.om_url + "/table/" + quote(om_name, safe=""),
                )
                if self.settings.om_lineage_enabled:
                    # This surface must be enabled only after version-specific contract tests.
                    lineage = self._get(
                        f"/lineage/table/{raw['id']}", {"upstreamDepth": 1, "downstreamDepth": 0}
                    )
                    visible_ids = {grant.source_names[f]: f for f in grant.columns}
                    asset.lineage = [
                        visible_ids[n["fullyQualifiedName"]]
                        for n in lineage.get("nodes", [])
                        if n.get("fullyQualifiedName") in visible_ids
                    ]
                assets.append(asset)
                contract = (raw.get("extension") or {}).get("edqContract")
                if contract:
                    contracts.append(json.loads(contract) if isinstance(contract, str) else contract)
                for tag in raw.get("tags", []):
                    if tag.get("source") == "Glossary":
                        term = self._get("/glossaryTerms/name/" + quote(tag["tagFQN"], safe=""))
                        definitions[term["fullyQualifiedName"]] = (term.get("description") or "")[:1000]
            # Contract contains approved metric formula, grain, as-of and reviewed join.
            # These cannot be inferred from descriptions, lineage, similarly named columns or an LLM.
            raw_context = {
                "assets": [a.model_dump() for a in assets],
                "metrics": [],
                "joins": [],
                "available_definitions": definitions,
                "synthetic": False,
                "capabilities": ["rest_tables", "columns", "owners", f"version:{version}"],
            }
            for contract in contracts:
                raw_context["metrics"].extend(contract.get("metrics", []))
                raw_context["joins"].extend(contract.get("joins", []))
            return restrict(
                MetadataContext.model_validate(raw_context), grant, self.settings.max_metadata_chars
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "metadata_unavailable",
                "OpenMetadata version or required API contract could not be verified.",
                503,
            ) from exc

    def health(self):
        try:
            return bool(self.verify_version())
        except Exception:
            return False
