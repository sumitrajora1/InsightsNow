# English Data Query System — Lending Data Desk

A working, deliberately bounded enterprise banking analytics vertical slice:

**Streamlit → FastAPI → identity + asset policy → authorized OpenMetadata context → typed LLM plan → semantic + SQL AST validation → Trino → bounded results → deterministic explanation / chart → Streamlit.**

FastAPI is the only orchestration/execution control point. The model has no database credentials or execution tools. Trino AI functions are not used.

## Run the synthetic demo

### Full stack: real Trino + PostgreSQL, fixture metadata + mock planner

Requirements: Docker Engine / Docker Desktop running, Compose v2, about 6 GB available memory and access to the public container/package registries.

```bash
cd english-data-query
python3 scripts/local_secrets.py   # writes ignored .env with random local credentials; never prints them
docker compose up --build -d
docker compose logs -f api
```

Open [Lending Data Desk](http://localhost:8501). API documentation: [OpenAPI UI](http://localhost:8000/docs). All exposed demo ports bind **127.0.0.1** only. This is explicitly **synthetic**, not a bank login or a production deployment.

Compose includes real Trino 484, PostgreSQL sessions/audit, and a separate synthetic source database with SELECT-only connector credentials. `demo_hive` and `demo_oracle` are **two PostgreSQL connector catalogs standing in for the enterprise sources**. They demonstrate real cross-catalog Trino execution, not real Hive or Oracle connectivity. The fixture OpenMetadata adapter and deterministic mock language planner are labelled in the UI and every result.

Demo identities:

| Identity | Scope |
|---|---|
| `alice` | Approved lending tables, AED / USD, all synthetic countries |
| `bob` | UAE / AED only, restricted in app policy and demo Trino row filters |
| `charlie` | No lending discovery/query access |

Try this sequence:

1. “Show corporate lending exposure by industry for the last six months.”
2. “Only show UAE.”
3. “Compare this with the previous six months.”
4. “What does exposure mean, and which data sources support this answer?”

Use “Show this as a trend” after the monthly query. A comparison has one average per period, so it uses grouped bars; it does not fabricate a monthly trend from period averages. Use the **Display** selector to change a suitable chart without re-executing SQL. Start a **New conversation** to reset analytical context.

### Offline application demo: no Docker or external credentials

This runs the same API, controls, persistence and UI with a clearly labelled **SQLite execution fallback**, not Trino. It is useful for development and acceptance tests.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install -e . --no-deps --no-build-isolation
python scripts/seed.py
alembic upgrade head
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
# in another terminal, same directory / virtual environment:
streamlit run ui/app.py --server.address=127.0.0.1 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
```

Python 3.12+ is supported by the project; the container and checked lock target Python 3.14. A writable `.runtime/` directory is required. Fixture generation seeds 36 synthetic facilities, 432 month-end snapshots, three industries, three countries and AED/USD balances. The fixture's complete reporting date is **31 October 2026**, not the current date. Re-running the seed script resets only the synthetic SQLite/source fixture, not sessions or bank data. Do not run it against live databases.

## What is implemented

| Module | Responsibility |
|---|---|
| `app/auth.py`, `app/enterprise.py` | Development/OIDC/LDAPS authentication, per-user engine authorization and discovery intersection |
| `app/metadata.py` | Bounded authorized fixture or version-pinned OpenMetadata REST retrieval |
| `app/llm.py` | Provider-neutral planner, deterministic demo grammar, bounded AWS Bedrock Converse adapter |
| `app/planning.py` | Reviewed lending semantics: snapshots, approved joins, currencies, periods and comparisons |
| `app/validation.py` | Trino-dialect AST checks, one SELECT, safe CTEs, physical identifiers, column resolution, approved functions, AST row limit |
| `app/execution.py` | Trino Python client and SQLite demo executor, user credentials, deadlines, cancellation and byte/row bounds |
| `app/conversation.py` | User-owned state, per-session lease, bounded jobs, permission rechecks and orchestration |
| `app/database.py` | SQLAlchemy persistence and retention, Alembic migrations in `migrations/` |
| `app/visualization.py` | Validated declarative KPI/bar/line/scatter/table selection |
| `app/explanation.py` | Deterministic, authorized-data-grounded explanations; no invented causality |
| `app/audit.py`, `app/observability.py` | Redacted trace events and content-free Prometheus metrics |
| `ui/app.py` | Thin chat/history/clarification/progress/cancellation/results/Plotly interface |

### Financial meaning

“Exposure” in this demo means **funded month-end outstanding balance**, excluding undrawn commitments. The facility key is unique; a facility/month snapshot is unique. Amounts sum only within a single snapshot date and currency. Six-month exposure is a sequence of six month-end totals, **not a sum across dates**. Comparing two six-month periods computes **the average monthly total per period** and returns `months_observed`. Incomplete periods are excluded. Minimum group size is enforced in SQL; no missing group is treated as zero. No FX conversion, risk-weighting, credit recommendation or causal business insight is generated.

The general SQL validator is intentionally narrower than arbitrary Trino SELECT syntax. In addition to AST checks, **execution requires equality with the approved metric compiler's SQL AST**. A model cannot introduce a valid-but-financially-wrong query, an undocumented join, a table function, or a free expression. To add metrics/grains/joins, update approved OpenMetadata contracts, the semantic compiler and tests together. Generic, unrestricted NL-to-SQL is outside this release.

### State and authorization

Compact state includes subject, metric, dimensions, country filter, currency, reporting window/grain, metadata references, validated SQL and query ID. It is persisted under issuer + subject ownership, not a browser-provided username in live mode. Concurrent follow-ups on one conversation return 409 to avoid lost updates. Every read and execution checks fresh grants. Changes invalidate stored result access. There is no shared user/result cache.

Questions, descriptions and result strings are untrusted data. SQL is parsed with SQLGlot, identifiers resolve only against authorized schema, functions are allowlisted, and values become AST literals rather than SQL text concatenation. Row bounds use an AST limit with a one-row lookahead; results also have byte/cell bounds. Prompt-based safeguards are not the authorization boundary.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/sessions`, `GET /v1/sessions` | Create/list only the authenticated user's conversations |
| `GET /v1/sessions/{id}` | Bounded authorized request history |
| `POST /v1/sessions/{id}/questions` | Submit typed question; returns 202 with request/correlation IDs |
| `GET /v1/requests/{id}` | Poll queued/running/clarification/completed/failed/timeout/cancelled state |
| `POST /v1/requests/{id}/cancel` | Cancel the owner's active request and attempt remote query cancellation |
| `POST /v1/requests/{id}/chart` | Validate a suitable display change against returned columns; no SQL rerun |
| `GET /v1/requests/{id}/audit` | Owner's redacted trace events |
| `/health/live`, `/health/ready`, `/metrics` | Liveness, dependencies and protected metrics |

Development calls require `X-Dev-User: alice` (or bob/charlie); live calls require a verified bearer access token in OIDC mode, or TLS-protected per-user credentials in the LDAPS profile. Never enable development headers in production. `EDQ_ENVIRONMENT=production` rejects fixture/mock/SQLite/development authentication and insecure Trino transport at startup. The OIDC UI mode reads an authenticated proxy access token; the LDAPS mode provides an HTTPS directory sign-in form with bounded server-memory credential retention.

Errors include safe codes/messages, not raw driver/model responses, SQL exceptions or credentials. Audit records never contain full result sets; SQL literals and raw question text are redacted by default. Session/request rows do contain bounded user-visible state/results: encrypt the database at rest and restrict its administrative access.

## Tests and evaluation

```bash
pytest
pytest --cov=app --cov-report=term-missing
ruff check --select F,I app ui scripts tests
python -m scripts.evaluate --url http://127.0.0.1:8000
# With the synthetic full stack running:
EDQ_RUN_TRINO_TESTS=1 pytest tests/test_real_trino.py
EDQ_RUN_UI_TESTS=1 pytest tests/test_ui.py
```

The 9 November full synthetic-stack retest passed **75 tests**, including the opt-in Trino and Streamlit cases. LDAPS/Ranger-specific tests use simulated transports; bank services remain unverified. See the verification record for measured coverage and live-service boundaries. The test suite exercises the real HTTP API, planner, semantic compiler, AST validator, SQL execution and persistence using a temporary synthetic SQLite database. It covers canonical output, context preservation, comparison accounting, definitions, ambiguous/missing metadata, unauthorized SQL/columns/functions, prompt-injection strings, user isolation, policy changes, chart changes, cancellation, concurrency, timeout, failed dependencies, empty/truncated data, audit failure and OIDC/metadata adapter contracts. `fixtures/evaluations.json` is the checked expected-behaviour dataset. The smoke evaluator runs the supported non-mutating cases against either offline or real-Trino API mode. Tests do not claim live Bedrock, enterprise OpenMetadata, Ranger, CDP, Oracle or OIDC-provider acceptance.

## LDAPS with existing CDP Ranger

An alternative to OIDC is now implemented: **Streamlit HTTPS sign-in → FastAPI → Trino password authentication → LDAPS**, with per-user Trino/Ranger authorization probes before metadata retrieval and the same identity for SQL execution. The deployment profile targets the supplied **CDP 7.1.7 SP1 / Trino 480** environment with Ranger already connected. Preserve the existing Ranger integration.

See [LDAPS/Ranger setup, credential handling and policy invalidation](docs/LDAPS_RANGER.md) and `deploy/ldaps-ranger/`. New tests use fake transports; live bank LDAPS/Ranger enforcement is not yet verified. A coordinated Ranger policy revision/invalidation process is required for stored results, especially when row filters or masks change. The synthetic local demo remains on its explicitly labelled development authentication.

## Live deployment and limitations

See [deployment guide](docs/DEPLOYMENT.md) for exact secrets, TLS, identity/JWT forwarding, OpenMetadata custom metric contracts and version probes, Trino/Ranger checks, network routes, OpenShift/Kubernetes templates, audit retention and acceptance gates. See [verification record](docs/VERIFICATION.md) for what was actually run on this host.

Important boundaries:

- Single backend worker/replica with bounded in-process jobs. A durable distributed queue is required before horizontal scaling. A process restart marks interrupted jobs failed rather than retrying them.
- Cancellation immediately signals execution; a model/metadata call in flight can only stop after its bounded HTTP timeout. Remote query cancellation can fail if Trino is unreachable; server-enforced resource/run-time limits remain essential.
- No automatic SQL execution retries. The Bedrock planner allows at most two model attempts, and every final SQL goes through the same validator. No broadly privileged service account fallback.
- External models receive no result rows. Deterministic explanations are the implemented sensitivity policy. General result narration/causality and arbitrary chart code are excluded.
- OpenMetadata REST, Bedrock and verified-token Trino adapters are implemented with contract tests but require actual endpoints, compatible server fields, approved metadata contracts and live credentials. No ontology, MCP or SDK feature is assumed.
- No bank-specific live compatibility, performance SLA, production certification or financial saving is asserted by a synthetic demo.

## Technical references used

- [OpenMetadata REST conventions and endpoint families](https://docs.open-metadata.org/v2.0.x/main-concepts/metadata-standard/apis)
- [Trino Python client: DBAPI and authentication](https://github.com/trinodb/trino-python-client)
- [Trino JWT authentication](https://trino.io/docs/current/security/jwt.html)
- [Trino file access controls](https://trino.io/docs/current/security/file-system-access-control.html) and [resource groups](https://trino.io/docs/current/admin/resource-groups.html)
- [Bedrock Converse API](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/converse.html)

These references inform adapter/configuration choices; live deployment still needs version-specific verification.
