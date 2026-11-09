# LDAPS sign-in with existing CDP Ranger authorization

Target supplied by the bank: **CDP 7.1.7 SP1, Apache Trino 480, Trino already integrated with CDP Ranger**. Preserve that existing integration and its approved policies. The files under `deploy/ldaps-ranger/` are examples to compare with the live configuration, not replacements for working cluster settings. No changes have been applied to the bank cluster.

## Implemented path

1. The user signs into Streamlit with a canonical directory username/password over the bank's HTTPS Route.
2. Streamlit calls FastAPI over HTTPS using those credentials. FastAPI authenticates through a TLS Trino connection; Trino's configured password authenticator performs the LDAPS bind. FastAPI verifies `current_user` equals the requested canonical name. There is no direct directory bind in Streamlit or FastAPI, and no OIDC dependency in this mode.
3. A small admin-controlled domain registry maps canonical usernames to discovery roles. This registry can narrow discovery; it cannot grant engine access. A missing user mapping yields no discovery privileges.
4. Before retrieving OpenMetadata, FastAPI intersects registry assets with fresh, user-authenticated zero-row SELECT probes in Trino. Probes explicitly reference all approved columns. If any column is denied, the **whole asset** is omitted. Service failures deny the request; there is no registry-only fallback. No source rows are returned by probes.
5. The normal metadata/planning/AST/semantic validation path runs. Final SQL uses the same user's Trino password authentication. Existing Ranger rules enforce access, filtering and masking at execution. No query runs as an application service user.
6. Polls/history/chart access repeat authorization checks. The approved policy revision also participates in the cached-result authorization fingerprint.

The application does not download/reimplement Ranger policies. Successful probes establish current engine permission, **not proof that the operator configured Ranger rather than another Trino access-control plugin**. Verify that the existing Trino 480 plugin is active and that its audit events reach the CDP audit destination.

## Identity, credentials and sessions

Set the API values in `deploy/ldaps-ranger/api.env.example`, plus the existing production database, OpenMetadata and Bedrock secrets. The mode requires:

```text
EDQ_AUTH_MODE=trino_ldaps
EDQ_AUTHORIZATION_PROVIDER=trino_ranger
EDQ_TRINO_AUTH=ldap_password
EDQ_TRINO_SCHEME=https
EDQ_EXECUTOR=trino
EDQ_LDAPS_IDENTITY_NAMESPACE=<stable-directory-identifier>
```

Use canonical short usernames. Match the Trino LDAP bind pattern and principal-to-user mapping to those names; configure the AD suffix in Trino, not in user-entered SQL or prompts. User substitution/aliases are rejected by the application's identity check. The stable directory namespace prevents conversation ownership from colliding with another directory. Application discovery roles are an explicit allowlist; they are not asserted to be live LDAP/Ranger group membership. Ranger's own group resolution remains authoritative for query access.

The UI stores a `SecretStr` password **in server-side session memory** for at most 15 minutes by default (configurable 1–30 minutes), clears it on logout/expiry and sends it only to the HTTPS API. `SecretStr` masks display; it is not memory encryption. The password is transiently held by API requests/query workers and passed to Trino over TLS. It is excluded from Identity serialization/repr and never written to session/audit tables or sent to the LLM. In-flight work can retain a reference until its bounded timeout; signing out does not forcibly cancel that work. Do not enable session-state persistence, memory dumps, HTTP credential/body tracing or debug capture on this deployment. Align this explicit password-in-memory design with the bank's security approval; credential-free delegation is a different adapter.

FastAPI reauthenticates on every request rather than issuing an application token. Invalid sign-ins are limited per user and globally in one process. Add ingress/WAF limits, AD lockout controls and source restrictions; do not horizontally scale this single-worker limiter. Ensure Trino authentication errors do not disclose directory details to users.

## Discovery and stored-result invalidation

Copy `deploy/ldaps-ranger/discovery.example.json` into a restricted ConfigMap. Replace its **synthetic asset mappings** with reviewed real SQL/OpenMetadata FQNs, approved columns and real role constraints; replace username placeholders. Supply a non-placeholder `ranger_policy_revision` such as a bank change ID. Keep the domain small (maximum eight assets, 64 columns per asset). Each probe and execution must fit the configured dependency/query limits; load-test LDAP and coordinator impact before adoption.

**Required operational dependency:** coordinate Ranger policy changes with the registry revision. Before changing any grant, group membership, row filter or mask, publish a new unique revision atomically with the new discovery constraints, block traffic during the change, and resume only after Trino has loaded the intended policy version. A changed revision blocks access to old stored results and changes in-flight grant fingerprints. New queries then run under the new engine policy.

The app does **not** automatically observe CDP Ranger policy versions. Zero-row probes detect denied SELECT permissions but cannot detect changed row predicates or masks when SELECT remains allowed. Uncoordinated policy changes can therefore leave an older stored result accessible. Production approval requires a bank-operated invalidation/change hook or a controlled change window; if this cannot be guaranteed, do not enable persisted-result access for production. The hook, Ranger cache propagation and freshness SLA need environment acceptance. This is not solved by LDAP sign-in or by setting a Trino username.

## Existing Trino 480 / Ranger configuration

Keep the deployed Ranger service, policy download authentication/keytabs, truststores and audit sinks. Check the examples against the official **480-tagged** source documentation:

- [Trino 480 LDAP configuration](https://github.com/trinodb/trino/blob/480/docs/src/main/sphinx/security/ldap.md)
- [Trino 480 Ranger configuration](https://github.com/trinodb/trino/blob/480/docs/src/main/sphinx/security/ranger-access-control.md)

For LDAPS, compare `password-authenticator.properties` with the current Trino coordinator config. Set the approved directory host, trusted CA and user bind pattern; refuse insecure LDAP. The example disables LDAP credential caching for revocation tests; assess directory load and bank lockout rules before rollout. Merge the TLS/PASSWORD fragment into the existing cluster config only if needed. Update discovery/internal HTTPS settings consistently before disabling HTTP.

For Ranger, verify the existing Trino service definition, exact plugin build and identity/group synchronization in CDP. The 480 source documents catalog/schema/table/column control, row filters, masks and auditing. It also requires query-execution and self-impersonation permissions. Limit self-impersonation to the user's own identity. Keep passthrough/functions/procedures and unapproved catalogs denied. The app uses cursor cancellation, not arbitrary kill-query procedures. Do not add application users to Ranger superuser/supergroup lists. Keep Hive/HDFS and Oracle source controls in place; their connector identities and impersonation semantics require separate checks.

The optional Ranger XML examples omit deployment-specific Admin authentication and audit destination credentials. They cannot establish a functioning audit pipeline on their own. Existing CDP 7.1.7 SP1 integration is an environment fact supplied by the bank, not a new product compatibility certification from this application. Do not install a new Ranger service definition or upgrade CDP as part of these application changes.

## OpenShift deployment

- Build/scan the updated image; retain one FastAPI replica/worker.
- Set `EDQ_UI_AUTH_MODE=ldaps`, a trusted HTTPS `EDQ_API_URL`, `EDQ_API_CA_BUNDLE` and optional `EDQ_UI_LDAPS_SESSION_SECONDS=900` on Streamlit. The OIDC proxy is not required for this profile. Expose the UI only through the approved TLS Route, preserving Streamlit's XSRF protection and authenticated application session behaviour.
- Use HTTPS for UI → API. For example, run Uvicorn with `--ssl-certfile /etc/edq/tls/tls.crt --ssl-keyfile /etc/edq/tls/tls.key`, mount the OpenShift TLS Secret read-only, and set probes to HTTPS. The production LDAPS adapter rejects plaintext API requests. Configure trusted proxy headers only for specific ingress addresses; never trust arbitrary client-provided identity headers.
- Mount CA bundles and the domain registry read-only. Mount only necessary Secrets. This API/UI does not need a Ranger Admin credential or a directory service-account password.
- Allow UI → API HTTPS; API → Trino HTTPS, PostgreSQL TLS, OpenMetadata/JWKS where used, Bedrock HTTPS and DNS. Separately allow Trino → directory LDAPS, CDP Ranger/audit endpoints and existing source dependencies. Do not open LDAP access from the UI.
- Apply the existing default-deny policies and restricted SCC guidance. Replace the OIDC-specific ingress path in the generic template with the approved LDAPS UI Route; do not simply expose the development deployment.

## Go/no-go acceptance on the bank cluster

Test using named, non-production directory accounts with an approved test dataset:

1. Correct password succeeds; wrong/blank/disabled/expired account and untrusted directory/API/Trino certificates fail. Verify exact username agreement across LDAP, Trino, Ranger and application audit.
2. Approved user sees the expected metadata; denied table or column never reaches the model. A denied column intentionally removes the entire asset. Metadata remains bounded and sensitivity policy applies.
3. Execute the same approved SQL from this app and an approved Trino client as the same user. Compare rows, country restrictions, masks, aggregate semantics and currency. Test particularly whether masking a metric changes/nulls its aggregation; do not label altered values as unmasked exposure.
4. Wrong-role and impersonation attempts fail; source writes, DDL and passthrough remain denied. Ranger and application logs identify the end user, not an app account.
5. Exercise logout/expiry, LDAP outage, Ranger policy-download outage/stale cache, certificate rotation, cancellation, concurrent requests and load. Ranger uses a policy cache: establish an acceptable stale-policy/failure strategy with the platform team; the app cannot infer a Ranger Admin outage from an engine that still authorizes against cached policy.
6. Change grants, group membership, row filters and masks using the coordinated revision process. Confirm old history/results and in-flight work are denied or withheld. Prove the invalidation hook and policy propagation before opening production access.
7. Inspect logs, database rows and model payloads for credential leakage; confirm retention and downstream audits.

Local tests use fake authenticated transports and policy responses. They verify application behaviour and configuration contracts, **not live LDAPS/CDP Ranger enforcement**. The earlier real-engine demo used Trino 484 with file rules and synthetic sources; it is not evidence for the bank's Trino 480 integration.
