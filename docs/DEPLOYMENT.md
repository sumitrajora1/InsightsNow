# Connecting real services

The delivered application is a **bounded lending vertical slice**, not an unrestricted enterprise SQL agent.
The manifest is a deployment template, not a claim of production approval or tested CDP/Ranger compatibility.

## 1. Environment and database

Build the image from the exact `requirements.lock`, scan it, and pin the digest. Use a managed PostgreSQL instance with TLS, backups and restricted application access. Run `alembic upgrade head` as a migration role before starting the API. Give the runtime role SELECT/INSERT/UPDATE on `conversations` and `query_requests`, and SELECT/INSERT plus sequence USAGE on `audit_events`. **Do not grant runtime DELETE/UPDATE on audit events.** Retention uses a separate maintenance role, never the runtime role. The local Compose provisioner demonstrates this separation.

Keep **one API worker and one replica**, `Recreate` rollout, until implementing a durable distributed job queue, shared cancellation signalling and per-user distributed admission control. This release uses database session leases and an in-process bounded executor. Rolling restarts interrupt jobs; startup marks them failed and never retries execution automatically. Query IDs / correlation IDs must be reconciled against Trino query logs when a worker crashes.

## 2. Enterprise sign-in

**Alternative implemented profile:** for the supplied CDP 7.1.7 SP1 / Trino 480 setup, use [LDAPS with existing CDP Ranger](LDAPS_RANGER.md). It replaces the OIDC-specific sign-in/JWT steps below, requires TLS per-user Trino password authentication, and preserves the existing Ranger deployment. The generic Kubernetes manifest defaults to OIDC; apply the LDAPS guide's environment/TLS changes before using that profile.


Place the UI behind an OIDC authorization-code / PKCE login proxy (for example, your approved oauth2-proxy deployment). Configure it to authenticate ordinary requests **and WebSocket handshakes**, remove all client-supplied identity/token headers, then set `X-Forwarded-Access-Token` on the authenticated Streamlit connection. Never make Streamlit directly reachable around that proxy. Configure secure, HTTP-only, SameSite cookies, session expiry, logout, CSRF protection and TLS at the ingress. No credential fields or token-paste workflow appear in the business UI.

Set `EDQ_UI_AUTH_MODE=oidc_proxy`; the UI forwards the access token only to the API. The API validates RS256/ES256 signature via pinned HTTPS JWKS, issuer, audience, expiry and required claims. Configure a flat `roles` claim (or set `EDQ_OIDC_ROLES_CLAIM` to the appropriate flat claim). Nested provider roles must be mapped by the identity provider. Subject ownership uses issuer + subject, not the display name. Do not use ID tokens as API access tokens.

For live execution, provision an access token whose audiences permit **both this API and the approved Trino JWT authentication deployment**, or implement a separately reviewed token-exchange adapter. Set `EDQ_TRINO_AUTH=jwt_passthrough`, `EDQ_TRINO_SCHEME=https` and `EDQ_TRINO_TOKEN_AUDIENCE`. Trino must verify the token, map its authenticated principal to the forwarded username, and deny unauthorized user impersonation. The API never falls back to a service account. A token valid only for this API is rejected before it is sent to Trino.

The shipped transport supports this explicitly configured JWT forwarding path. It does **not** implement Kerberos constrained delegation, OAuth token exchange or arbitrary client-credentials impersonation. Configure a supported alternative before using an environment that requires one.

## 3. Permissions and Ranger

Mount an administrator-controlled `config/policy.json` mapping identity roles to fully qualified tables and columns, currencies and an optional country restriction. Replace synthetic role grants with reviewed enterprise grants. This file is the app's discovery allowlist, not a substitute for source/engine authorization. Every question, poll, history read, chart switch and audit read reloads it. A changed authorization fingerprint makes old results inaccessible until the user runs a new request. No cross-user cache exists.

Enforce the same or stricter controls in Trino and its sources. Validate the **actual** Trino/SEP release, CDP parcel and hotfix, Hive Metastore, HDFS authentication and Ranger plugin/service-definition combination. Check denied catalogs/columns, row filtering, masking, metadata visibility, passthrough functions, user substitution and audit delivery for each supported client path. Existing Hive/Ranger policies do not automatically apply to a new Trino service. Merely setting `user=` is **not authentication** and does not establish Ranger enforcement.

The demo uses Trino file-based read-only access rules and separate SELECT-only PostgreSQL credentials, including Bob's UAE/AED table filters. It is loopback-only and deliberately permits development usernames; never expose it as a bank identity system. Live Hive/CDP and Oracle catalogs are configured on Trino, outside the LLM. Mount credentials/keytabs using the platform secret mechanism, configure TLS/Kerberos/HDFS impersonation as required, and validate transactional reads, views, dates, decimals and supported versions. No certified compatibility between specific CDP and Trino releases is claimed here.

## 4. OpenMetadata contract

Set `EDQ_METADATA_PROVIDER=openmetadata`, HTTPS URL, bot token and **explicit tested version prefix** (`EDQ_OM_EXPECTED_VERSION`, e.g. a version actually installed in your environment). Startup readiness checks `/api/v1/system/version`. Retrieval checks the version again, then fetches **only** mapped authorized domain tables using `/api/v1/tables/name/{fullyQualifiedName}`. Table columns, owners, tags and extension fields must satisfy the response contract. Glossary tags can resolve through `/api/v1/glossaryTerms/name/{name}`. No SDK, ontology graph or MCP capability is assumed.

Before enabling the adapter, verify these endpoints and supported `fields` values against your installed server's OpenAPI. Configure `EDQ_OM_TABLE_FIELDS` to the tested field set (default `owners,tags,tableConstraints,extension`). The adapter fails closed on a mismatched version, missing required shape or failed request; it does not silently fall back to fixtures. Additional API families need contract tests. `EDQ_OM_LINEAGE_ENABLED=false` is the default; enable only after testing `/api/v1/lineage/table/{id}` on that version. Absent lineage stays absent. Do not infer valid joins from lineage or matching column names.

Create an approved table custom property `extension.edqContract` in OpenMetadata. Its JSON contains `metrics` and `joins` following `Metric` / `JoinDefinition` in `app/contracts.py`. A JSON string custom property is also accepted. Copy the structure from `fixtures/metadata.json`, **not its synthetic values**: replace asset IDs with real table IDs, source columns with approved names, definition/formula, reporting currency, complete-data `as_of`, minimum group size, reviewed many-to-one join and glossary reference. This is required because generic column descriptions are insufficient to establish snapshot accounting semantics. Only metadata admins may change it; protect that property with approval/versioning. Do not put an unverified formula or date in the model prompt as a substitute.

`config/policy.json` maps the approved **SQL FQN** (catalog.schema.table) to its **OpenMetadata FQN**, which may include a service/database prefix. No SQL name is fabricated by stripping arbitrary FQN parts. Only eight assets and a bounded context are retrieved for this initial domain. Expand domains through reviewed contracts and compiler changes, not an unbounded global bot-token search. Restrict the bot token to the same approved domain. The adapter's ACL intersection happens before any metadata reaches a model or user.

## 5. Bedrock and explanations

Use workload identity/short-lived AWS credentials and only the approved model/inference profile. Set region, model ID and optional private endpoint. Model must support Converse text responses. The provider-neutral `Planner` protocol returns a strict Pydantic `QueryPlan`. The Bedrock adapter uses bounded JSON planning and at most two total model attempts; when SQL differs from the reviewed semantic compiler, it provides the exact approved SQL for correction. No model response executes before the common SQL validator. Unsupported model responses fail closed.

Map the installed OpenMetadata sensitivity tags through `EDQ_OM_CLASSIFICATION_MAP` (JSON object). Unrecognized or absent tags are `unclassified`, not implicitly public. The adapter reads table and column classifications. `EDQ_MODEL_METADATA_SENSITIVITIES` defaults to `["public","internal"]`; an unapproved table or column classification blocks the model call before transport. Approve any change through the bank's data policy process.

Questions, compact state and authorized metadata go to Bedrock. Results **never** go to the external model in this release; explanations are deterministic and grounded in approved metadata and returned result shape. This is the implemented sensitivity policy, not a configurable promise to transmit sensitive rows. Adding result-sharing or generative explanations requires a separate reviewed redaction/consent adapter and tests. Prompts do not contain credentials or complete chat histories. Bedrock model calls may process question/metadata content: data residency, private connectivity, model access and provider policies require bank approval.

## 6. Network, resources and ingress

Use the YAML as a starting point. Create `edq-api-secrets` from the approved secret manager (database URL and OpenMetadata token; AWS identity should be federated). Create `edq-policy` from the reviewed policy file. Do not commit Secret values. Install trust bundles for private certificates. Add a reviewed migration Job using a separate database role. Add a maintenance CronJob running `python -m scripts.purge` with the retention role and an approved `EDQ_RETENTION_DAYS`.

Default-deny policy intentionally blocks unconfigured dependencies. Add narrowly scoped egress for API → PostgreSQL, OpenMetadata HTTPS, OIDC JWKS HTTPS, Bedrock/private AWS HTTPS and Trino HTTPS, plus cluster DNS. The UI needs only API and DNS. Allow ingress to the UI **only from the authenticated login proxy**; the API remains internal unless separately protected by an authenticated gateway. Set WebSocket timeouts and body-size limits. Use service-mesh mTLS for UI→API and dependency hops or equivalent end-to-end TLS. On OpenShift, use the restricted SCC and a re-encrypt/passthrough Route to the approved authentication proxy; do not expose the raw Streamlit Service with a bypass Route.

Tune worker limits, Trino resource groups, maximum run time, queues, source concurrency and memory based on load tests. The application imposes row lookahead, byte/cell bounds, global/per-user job limits, no query retries and cancellation. Trino must also enforce limits because application failure must not create unlimited queries. Choose catalog-level SELECT-only identities and deny table/procedure passthrough routes. The sample resource-group settings are **demo settings**, not production sizing.

## 7. Operations and approval

`/health/live` checks process liveness; `/health/ready` checks SQL store, metadata version/fixture and query-engine reachability. It does not bill a model invocation or certify user-specific authorization. Alert on real query outcomes, rejected plans and model failures. Protect `/metrics` behind operations access; it contains bounded labels and no user, SQL or result content.

Audit records include owner, session-linked request, correlation ID, metadata references, model/prompt version, redacted generated/validated SQL, decisions, query ID, timings and row counts. Raw question text is off by default. Do not log HTTP authorization headers or request bodies at the ingress/APM. Session/request records contain bounded user-visible query state/results and must use database-at-rest encryption, strict DBA access, backups and approved retention. This application does not implement application-layer envelope encryption or tamper-evident/WORM logging. Export append-only audit events to the bank's protected audit store and validate failure handling before production approval.

Acceptance must include provider contract tests, exact SQL outputs, disclosure/denial cases, revoked access, worker restarts, credential expiry, cancellation, timeout, load, backup recovery and source-impact checks. A successful local synthetic demo does not establish production readiness.
