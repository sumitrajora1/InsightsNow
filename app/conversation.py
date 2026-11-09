"""Persisted analytical state plus a bounded single-process job coordinator.

Production must run ONE backend worker/replica until replaced by a shared job queue.
Database leases prevent concurrent follow-ups from overwriting one conversation.
"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app import observability as metrics
from app.audit import digest, redact_sql
from app.auth import Policy
from app.contracts import AnalyticalState, QueryResponse
from app.database import Store
from app.errors import AppError
from app.execution import ExecutionControl
from app.explanation import explain
from app.llm import PROMPT_VERSION
from app.planning import prepare_state
from app.validation import SQLValidator
from app.visualization import chart_options, choose_chart


def grant_fingerprint(grant):
    return hashlib.sha256(
        json.dumps(
            {
                "assets": {k: sorted(v) for k, v in grant.columns.items()},
                "country": grant.country,
                "currencies": sorted(grant.currencies),
                "source_names": grant.source_names,
                "authorization_revision": grant.authorization_revision,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


class QueryService:
    def __init__(self, settings, store: Store, policy: Policy, metadata, planner, executor):
        self.settings, self.store, self.policy = settings, store, policy
        self.metadata, self.planner, self.executor = metadata, planner, executor
        self.validator = SQLValidator(settings.max_rows)
        self.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs, thread_name_prefix="edq")
        self.lock = threading.Lock()
        self.active = {}

    def submit(self, identity, session_id, question):
        conversation = self.store.conversation(identity.owner, session_id)
        grant = self.policy.grant(identity)
        response = QueryResponse(
            id=str(uuid4()),
            session_id=session_id,
            correlation_id=str(uuid4()),
            question=question,
            status="queued",
            stage="queued",
        )
        with self.lock:
            if (
                len(self.active) >= self.settings.max_concurrent_jobs
                or sum(o == identity.owner for o, _ in self.active.values()) >= self.settings.max_user_jobs
            ):
                raise AppError("busy", "The query capacity is busy. Try again shortly.", 429)
            self.store.reserve(response, identity.owner, grant_fingerprint(grant))
            control = ExecutionControl(self.settings.query_timeout_seconds)
            self.active[response.id] = (identity.owner, control)
        try:
            self.store.audit(
                identity.owner,
                response,
                "request_received",
                {
                    "question_hash": digest(question),
                    "question": question if self.settings.audit_store_text else "[REDACTED]",
                    "model": self.planner.model_id,
                    "prompt_version": PROMPT_VERSION,
                },
            )
            previous = AnalyticalState.model_validate(conversation.state) if conversation.state else None
            self.pool.submit(self._run, identity, response, previous, control)
        except Exception:
            with self.lock:
                self.active.pop(response.id, None)
            response.status, response.stage = "failed", "audit_unavailable"
            self.store.finish(identity.owner, response, None)
            raise AppError("audit_unavailable", "The audit record could not be written. No query ran.", 503)
        return response

    def _stage(self, owner, response, stage):
        response.stage, response.status = stage, "running"
        self.store.save_request(owner, response)

    def _run(self, identity, response, previous, control):
        accepted_state = None
        try:
            control.check()
            self._stage(identity.owner, response, "metadata")
            grant = self.policy.grant(identity)
            self._assert_grant(identity, response.id, grant)
            with metrics.STAGES.labels("metadata").time():
                context = self.metadata.retrieve(response.question, grant)
            control.check()
            response.metadata = context
            self.store.audit(
                identity.owner,
                response,
                "metadata_retrieved",
                {
                    "asset_refs": [a.id for a in context.assets],
                    "synthetic": context.synthetic,
                    "asset_versions": {a.id: a.version for a in context.assets},
                },
            )
            self._stage(identity.owner, response, "planning")
            classifications = [a.sensitivity for a in context.assets] + [
                c.sensitivity for a in context.assets for c in a.columns
            ]
            if self.settings.llm_provider == "bedrock" and any(
                level not in self.settings.model_metadata_sensitivities for level in classifications
            ):
                raise AppError(
                    "model_policy_denied",
                    "This metadata classification is not approved for the external model.",
                    403,
                )
            metrics.MODELS.labels(self.settings.llm_provider).inc()
            with metrics.STAGES.labels("model").time():
                plan = self.planner.plan(response.question, context, previous, grant)
            control.check()
            response.plan = plan
            self.store.audit(
                identity.owner,
                response,
                "plan_proposed",
                {
                    "generated_sql": redact_sql(plan.sql),
                    "kind": plan.kind,
                    "metadata_refs": plan.metadata_refs,
                    "model": self.planner.model_id,
                    "prompt_version": PROMPT_VERSION,
                },
            )
            if plan.kind == "clarification":
                response.clarification = plan.clarification or "Please clarify the approved metric or period."
                response.status, response.stage = "clarification", "complete"
                self.store.audit(
                    identity.owner, response, "clarification_requested", {"kind": "metric_or_scope"}
                )
                if plan.state.pending_clarification == "period":
                    accepted_state = plan.state
            else:
                normalized = prepare_state(plan.state, context, grant)
                plan.state = normalized
                plan.metadata_refs = normalized.metadata_refs
                if plan.kind == "definition":
                    response.explanation = explain(normalized, context)
                    response.status, response.stage = "completed", "complete"
                    accepted_state = (
                        previous  # Definition lookup does not change existing analytical context.
                    )
                else:
                    self._stage(identity.owner, response, "validating")
                    with metrics.STAGES.labels("validation").time():
                        validated = self.validator.validate(
                            plan.sql or "", plan.parameters, context, grant, plan
                        )
                    control.check()
                    self._assert_grant(identity, response.id, self.policy.grant(identity))
                    self.store.audit(
                        identity.owner,
                        response,
                        "sql_approved",
                        {
                            "validated_sql": redact_sql(validated),
                            "sql_hash": digest(validated),
                            "decision": "allow",
                        },
                    )
                    self._stage(identity.owner, response, "executing")
                    with metrics.STAGES.labels("execution").time():
                        result = self.executor.execute(validated, identity, control, response.correlation_id)
                    control.check()
                    self._assert_grant(identity, response.id, self.policy.grant(identity))
                    for col in result.columns:
                        if "exposure" in col.name:
                            col.unit = normalized.currency
                    result.allowed_charts = chart_options(result)
                    preferred = normalized.chart_preference
                    # Automatic fall-back is explicit in the explanation; chart endpoint rejects unsuitable switches.
                    if preferred not in result.allowed_charts:
                        preferred = "auto"
                    result.chart = choose_chart(result, preferred)
                    response.result = result
                    response.explanation = explain(normalized, context, result)
                    normalized.last_sql, normalized.last_query_id = validated, result.query_id
                    plan.state = normalized
                    accepted_state = normalized
                    response.status, response.stage = "completed", "complete"
                    self.store.audit(
                        identity.owner,
                        response,
                        "execution_completed",
                        {
                            "query_id": result.query_id,
                            "duration_ms": result.duration_ms,
                            "rows": result.row_count,
                            "truncated": result.truncated,
                            "backend": result.execution_backend,
                        },
                    )
        except AppError as exc:
            response.status = (
                "cancelled" if exc.code == "cancelled" else "timeout" if exc.code == "timeout" else "failed"
            )
            response.stage = "complete"
            response.error = {"code": exc.code, "message": exc.message}
            response.result = None
            metrics.ERRORS.labels(exc.code).inc()
            try:
                query_id = control.query_id() if control.query_id else None
                self.store.audit(
                    identity.owner,
                    response,
                    "request_rejected_or_failed",
                    {"code": exc.code, "query_id": query_id},
                )
            except Exception:
                response.error = {
                    "code": "audit_unavailable",
                    "message": "Audit persistence failed. Contact operations with the correlation ID.",
                }
        except Exception:
            response.status, response.stage = "failed", "complete"
            response.result = None
            response.error = {
                "code": "internal_error",
                "message": "The request could not complete. Contact support with the correlation ID.",
            }
            metrics.ERRORS.labels("internal_error").inc()
            try:
                self.store.audit(identity.owner, response, "request_failed", {"code": "internal_error"})
            except Exception:
                pass
        finally:
            metrics.REQUESTS.labels(response.status).inc()
            try:
                self.store.finish(
                    identity.owner,
                    response,
                    accepted_state if response.status in ("completed", "clarification") else None,
                )
            finally:
                with self.lock:
                    self.active.pop(response.id, None)

    def _assert_grant(self, identity, request_id, grant):
        if self.store.get_request(identity.owner, request_id).grant_hash != grant_fingerprint(grant):
            raise AppError(
                "permissions_changed",
                "Permissions changed. Start a fresh request; stored results are unavailable.",
                403,
            )

    def get(self, identity, request_id):
        grant = self.policy.grant(identity)
        self._assert_grant(identity, request_id, grant)
        response = QueryResponse.model_validate(self.store.get_request(identity.owner, request_id).data)
        # Permission grants are reloaded on every retrieval. Re-resolve current metadata before exposing stored data.
        if response.metadata is not None:
            context = self.metadata.retrieve(response.question, grant)
            old = {a.fqn: {c.name for c in a.columns} for a in response.metadata.assets}
            new = {a.fqn: {c.name for c in a.columns} for a in context.assets}
            if any(not columns <= new.get(name, set()) for name, columns in old.items()):
                raise AppError("permissions_changed", "The stored result is no longer authorized.", 403)
        return response

    def cancel(self, identity, request_id):
        # Ownership alone permits stopping one's own job, even when metadata is down or access was revoked.
        response = QueryResponse.model_validate(self.store.get_request(identity.owner, request_id).data)
        with self.lock:
            entry = self.active.get(request_id)
        if entry:
            self.store.audit(identity.owner, response, "cancellation_requested", {})
            entry[1].cancel()
        return {"id": request_id, "cancellation_requested": bool(entry)}

    def close(self):
        with self.lock:
            controls = [c for _, c in self.active.values()]
        for c in controls:
            c.cancel()
        self.pool.shutdown(wait=True, cancel_futures=False)
