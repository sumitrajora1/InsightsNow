# Verification record

Verified on 6 November 2026 on macOS ARM64 with Python 3.14.6. All data was synthetic.
No connection was made to bank CDP/Hive, Oracle, enterprise OpenMetadata, an OIDC identity provider or AWS Bedrock.

## Executed checks

| Check | Result |
|---|---|
| Automated application tests | 53 passed in the default run; all **57 passed** with Trino and UI integration cases enabled; **86%** measured app coverage |
| Real Trino integration tests | Three passed against native Apache Trino 484 and PostgreSQL |
| Streamlit integration test | Repeated identical chat questions, result tables, Plotly charts and new conversation exercised against running API; result view also inspected in the browser |
| Seven-case HTTP evaluation | Monthly exposure, country follow-up, equal-period comparison, trend, definition, ambiguity and injection rejection passed with `execution_backend=trino` for executed queries |
| Source and audit privileges | PostgreSQL source role denied DELETE; runtime role denied UPDATE and DELETE of audit events |
| Static checks | Ruff formatting/lint checks and dependency consistency passed |
| Deployment configuration | Docker Compose configuration validated; Docker image build/Compose startup not run because Docker daemon was unavailable |

The real-engine tests check query IDs and audit correlation, chart switches without re-execution, cross-user denial, direct Trino row filtering, unauthorized identity denial and DDL denial. Application tests additionally cover missing metadata, semantic SQL checks, unsupported functions/columns, malicious model output, external-model metadata sensitivity policy, truncated/empty data, cancellation, deadlines, failed dependencies, admission limits and audit-write failure.

## Actual engine/data setup

A local Trino **484** server and a local PostgreSQL instance were started independently of Docker for verification. Both Trino catalogs use the PostgreSQL connector: `demo_hive.lending.exposure_snapshots` and `demo_oracle.lending.facilities`. This is a working cross-catalog query demonstration, **not** verification of the Hive or Oracle connectors. Native services are bound to loopback. Native PostgreSQL was an isolated synthetic test instance; use the password-protected Compose setup or bank-approved database authentication for repeatable deployment.

The application runtime role is `edq_app`; the source reader is `edq_reader`. PostgreSQL persisted conversations, requests and append-only application audit events. Alembic migration `0001` was applied before using those runtime roles.

36 synthetic facilities and 432 month-end snapshots cover November 2025–October 2026. The canonical last-six-month query returns 18 rows (six dates × three industries), with AED Manufacturing exposure of **906,000** for 31 May 2026. The UAE follow-up returns **222,000** for that date/industry. Its current six-month average is **227,000**, compared with **215,000** in the previous six complete months. These are fixture checks, not banking findings.

## Reproduce

Run the stack using the README, then:

```bash
pytest
python -m scripts.evaluate
EDQ_RUN_TRINO_TESTS=1 pytest tests/test_real_trino.py
EDQ_RUN_UI_TESTS=1 pytest tests/test_ui.py
```

The opt-in tests require the running **synthetic** API on port 8000 and Trino on 8080. Use `EDQ_TEST_API_URL`, `EDQ_TEST_TRINO_HOST` and `EDQ_TEST_TRINO_PORT` to change the real-stack test endpoints; use `EDQ_API_URL` for the UI test. They create test conversations and issue read-only synthetic queries. The DDL-denial test attempts to create a `must_never_exist` table and must be run only with the supplied demo access rules.

## Implemented but requiring live environment acceptance

- OpenMetadata REST adapter: HTTP contract tests cover version probing, bounded authorized tables and metric-contract parsing. The installed server's API fields, classification names, glossary, approved metric contracts and optional lineage must be verified.
- Bedrock Converse adapter: structured JSON, two-attempt maximum and approved SQL correction are tested with a fake transport. A real model, IAM identity, region/private endpoint and bank policy approval remain necessary.
- OIDC: signed JWT validation, issuer, audience and expiry are tested with generated keys. The real login proxy, IdP claims and WebSocket authentication remain to be configured.
- Trino JWT forwarding: fail-closed/no-service-account fallback is tested. Real TLS, token audience, principal/username mapping and engine/source authorization must be accepted together.
- Hive/CDP, Oracle and Ranger: no compatibility or enforcement claim. Verify exact releases, patches, connector configuration, authentication, masking/row policies and audit delivery in the bank environment.
- Kubernetes/OpenShift YAML: deployment guidance/template, not applied to a cluster. Supply reviewed image digests, secrets, TLS, network rules, migration/retention jobs and production load/recovery tests.

Run one API process/replica. Distributed jobs, horizontally scaled cancellation, application-layer encryption and tamper-evident audit export are not implemented. Explanations are deterministic; no result rows are sent to an external model.

## 7 November 2026 — LDAPS / existing CDP Ranger extension

Target supplied by the user: CDP 7.1.7 SP1 and Trino 480, already connected to CDP Ranger. The existing cluster was not modified. Trino 480 tagged LDAP and Ranger documentation was inspected; those files inform the example configuration, not a certification of the CDP stack.

The updated default test run passed **71 tests**, with **87%** measured app coverage. Four opt-in external-stack tests were skipped in this update run. Eighteen new cases cover TLS-only configuration, malformed/empty credentials, canonical identity agreement, credential exclusion from serialized identity/database/audit, bounded failed-login throttling, engine authorization intersection and revocation, policy-revision invalidation, no service-account fallback, per-user password execution, HTTPS enforcement, and Streamlit sign-in/logout/expiry. Authenticated transports and Ranger denials were faked; no bank directory or Ranger was contacted. Ruff checks passed.

See [LDAPS/Ranger guide](LDAPS_RANGER.md) for the required policy-change invalidation process. Row-mask/filter changes are not automatically observed from CDP; deployment requires a coordinated revision hook/change window. No claim of live LDAPS, bank Ranger enforcement, or Trino 480 execution acceptance is made by these tests. The previous real Trino execution record above remains a separate Trino 484 synthetic demonstration.

## 9 November 2026 — full synthetic retest

Restarted the local native Trino 484 server, FastAPI and Streamlit against the existing synthetic PostgreSQL instance. API readiness and UI health both returned HTTP 200. The complete suite with `EDQ_RUN_UI_TESTS=1 EDQ_RUN_TRINO_TESTS=1 pytest` passed **75 tests** with no skipped cases. The seven-case HTTP evaluator also passed: monthly query, country follow-up, comparison, trend, definition, ambiguity and unsafe-request rejection. A fresh empty Alice conversation was prepared for interactive testing.

LDAPS/Ranger adapter cases still use fake authenticated transports; this retest did not connect to bank Trino 480, CDP 7.1.7 SP1, directory or Ranger services. Direct observation of the user's browser tab was blocked by the browser security policy. Streamlit application tests passed independently; no browser workaround was attempted. One upstream Starlette test-client deprecation warning remains.
